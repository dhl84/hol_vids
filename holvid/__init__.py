"""hol_vids — turn a folder of raw vacation clips into a titled, review-ready
Final Cut Pro timeline. Fully local; needs ffmpeg/ffprobe on PATH.

Pipeline:  probe -> sheets -> (review.json) -> [upright] -> build
"""
from .config import Config

__all__ = ["Config"]
