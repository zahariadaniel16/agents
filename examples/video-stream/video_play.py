import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterable, Union

import av
import numpy as np
from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import JobContext, WorkerOptions, cli, utils
from livekit.agents.utils.av_sync import AVSynchronizer

# Load environment variables
load_dotenv()

logger = logging.getLogger(__name__)


@dataclass
class MediaInfo:
    video_width: int
    video_height: int
    video_fps: float
    audio_sample_rate: int
    audio_channels: int


class MediaFileStreamer:
    """Streams video and audio frames from a media file in an endless loop."""

    def __init__(self, media_file: Union[str, Path]) -> None:
        self._media_file = str(media_file)
        # Create separate containers for each stream
        self._video_container = av.open(self._media_file)
        self._audio_container = av.open(self._media_file)
        self._stopped = False

        # Cache media info
        video_stream = self._video_container.streams.video[0]
        audio_stream = self._audio_container.streams.audio[0]
        self._info = MediaInfo(
            video_width=video_stream.width,
            video_height=video_stream.height,
            video_fps=float(video_stream.average_rate),  # type: ignore
            audio_sample_rate=audio_stream.sample_rate,
            audio_channels=audio_stream.channels,
        )

    @property
    def info(self) -> MediaInfo:
        return self._info

    async def stream_video(self) -> AsyncIterable[rtc.VideoFrame]:
        """Streams video frames from the media file in an endless loop."""
        while not self._stopped:
            self._video_container.seek(0)  # Seek back to start
            for av_frame in self._video_container.decode(video=0):
                if self._stopped:
                    break
                # Convert video frame to RGBA
                frame = av_frame.to_rgb().to_ndarray()
                frame_rgba = np.ones(
                    (frame.shape[0], frame.shape[1], 4), dtype=np.uint8
                )
                frame_rgba[:, :, :3] = frame
                yield rtc.VideoFrame(
                    width=frame.shape[1],
                    height=frame.shape[0],
                    type=rtc.VideoBufferType.RGBA,
                    data=frame_rgba.tobytes(),
                )

    async def stream_audio(self) -> AsyncIterable[rtc.AudioFrame]:
        """Streams audio frames from the media file in an endless loop."""
        while not self._stopped:
            self._audio_container.seek(0)  # Seek back to start
            for av_frame in self._audio_container.decode(audio=0):
                if self._stopped:
                    break
                # Convert audio frame to raw int16 samples
                frame = av_frame.to_ndarray().T  # Transpose to (samples, channels)
                frame = (frame * 32768).astype(np.int16)
                yield rtc.AudioFrame(
                    data=frame.tobytes(),
                    sample_rate=self.info.audio_sample_rate,
                    num_channels=frame.shape[1],
                    samples_per_channel=frame.shape[0],
                )

    async def aclose(self) -> None:
        """Closes the media container and stops streaming."""
        self._stopped = True
        self._video_container.close()
        self._audio_container.close()


async def entrypoint(job: JobContext):
    await job.connect()
    room = job.room

    # Create media streamer
    # Should we add a sample video file?
    media_path = "/path/to/video.mp4"
    streamer = MediaFileStreamer(media_path)
    media_info = streamer.info

    # Create video and audio sources/tracks
    queue_size_ms = 1000  # 1 second
    video_source = rtc.VideoSource(
        width=media_info.video_width,
        height=media_info.video_height,
    )
    print(media_info)
    audio_source = rtc.AudioSource(
        sample_rate=media_info.audio_sample_rate,
        num_channels=media_info.audio_channels,
        queue_size_ms=queue_size_ms,
    )

    video_track = rtc.LocalVideoTrack.create_video_track("video", video_source)
    audio_track = rtc.LocalAudioTrack.create_audio_track("audio", audio_source)

    # Publish tracks
    video_options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_CAMERA)
    audio_options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)

    await room.local_participant.publish_track(video_track, video_options)
    await room.local_participant.publish_track(audio_track, audio_options)

    @utils.log_exceptions(logger=logger)
    async def _push_video_frames(
        video_stream: AsyncIterable[rtc.VideoFrame], av_sync: AVSynchronizer
    ) -> None:
        """Task to push video frames to the AV synchronizer."""
        async for frame in video_stream:
            await av_sync.push(frame)
            await asyncio.sleep(0)

    @utils.log_exceptions(logger=logger)
    async def _push_audio_frames(
        audio_stream: AsyncIterable[rtc.AudioFrame], av_sync: AVSynchronizer
    ) -> None:
        """Task to push audio frames to the AV synchronizer."""
        async for frame in audio_stream:
            await av_sync.push(frame)
            await asyncio.sleep(0)

    try:
        av_sync = AVSynchronizer(
            audio_source=audio_source,
            video_source=video_source,
            video_fps=media_info.video_fps,
            video_queue_size_ms=queue_size_ms,
        )

        # Create and run video and audio streaming tasks
        video_stream = streamer.stream_video()
        audio_stream = streamer.stream_audio()

        video_task = asyncio.create_task(_push_video_frames(video_stream, av_sync))
        audio_task = asyncio.create_task(_push_audio_frames(audio_stream, av_sync))

        # Wait for both tasks to complete
        await asyncio.gather(video_task, audio_task)
        await av_sync.wait_for_playout()

    finally:
        await streamer.aclose()
        await av_sync.aclose()


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            job_memory_warn_mb=400,
        )
    )
