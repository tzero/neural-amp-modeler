# File: data.py
# Created Date: Saturday February 5th 2022
# Author: Steven Atkinson (steven@atkinson.mn)

import abc
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Sequence, Tuple, Union

import numpy as np
import torch
import wavio
from torch.utils.data import Dataset as _Dataset

from ._core import InitializableFromConfig

_REQUIRED_SAMPWIDTH = 3
_REQUIRED_RATE = 48_000
_REQUIRED_CHANNELS = 1  # Mono


class Split(Enum):
    TRAIN = "train"
    VALIDATION = "validation"


@dataclass
class WavInfo:
    sampwidth: int
    rate: int


def wav_to_np(
    filename: Union[str, Path],
    require_match: Optional[Union[str, Path]] = None,
    required_shape: Optional[Tuple[int]] = None,
    required_wavinfo: Optional[WavInfo] = None,
    preroll: Optional[int] = None,
    info: bool = False,
) -> Union[np.ndarray, Tuple[np.ndarray, WavInfo]]:
    """
    :param preroll: Drop this many samples off the front
    """
    x_wav = wavio.read(str(filename))
    assert x_wav.data.shape[1] == _REQUIRED_CHANNELS, "Mono"
    assert x_wav.sampwidth == _REQUIRED_SAMPWIDTH, "24-bit"
    assert x_wav.rate == _REQUIRED_RATE, "48 kHz"

    if require_match is not None:
        assert required_shape is None
        assert required_wavinfo is None
        y_wav = wavio.read(str(require_match))
        required_shape = y_wav.data.shape
        required_wavinfo = WavInfo(y_wav.sampwidth, y_wav.rate)
    if required_wavinfo is not None:
        if x_wav.rate != required_wavinfo.rate:
            raise ValueError(
                f"Mismatched rates {x_wav.rate} versus {required_wavinfo.rate}"
            )
    arr_premono = x_wav.data[preroll:] / (2.0 ** (8 * x_wav.sampwidth - 1))
    if required_shape is not None:
        if arr_premono.shape != required_shape:
            raise ValueError(
                f"Mismatched shapes {arr_premono.shape} versus {required_shape}"
            )
        # sampwidth fine--we're just casting to 32-bit float anyways
    arr = arr_premono[:, 0]
    return arr if not info else (arr, WavInfo(x_wav.sampwidth, x_wav.rate))


def wav_to_tensor(
    *args, info: bool = False, **kwargs
) -> Union[torch.Tensor, Tuple[torch.Tensor, WavInfo]]:
    out = wav_to_np(*args, info=info, **kwargs)
    if info:
        arr, info = out
        return torch.Tensor(arr), info
    else:
        arr = out
        return torch.Tensor(arr)


def tensor_to_wav(
    x: torch.Tensor,
    filename: Union[str, Path],
    rate: int = 48_000,
    sampwidth: int = 3,
    scale="none",
):
    wavio.write(
        filename,
        (torch.clamp(x, -1.0, 1.0) * (2 ** (8 * sampwidth - 1)))
        .detach()
        .cpu()
        .numpy()
        .astype(np.int32),
        rate,
        scale=scale,
        sampwidth=sampwidth,
    )


class AbstractDataset(_Dataset, abc.ABC):
    @abc.abstractmethod
    def __getitem__(self, idx) -> Tuple[torch.Tensor, torch.Tensor]:
        pass


class Dataset(AbstractDataset, InitializableFromConfig):
    """
    Take a pair of matched audio files and serve input + output pairs
    """

    def __init__(
        self,
        x: torch.Tensor,
        y: torch.Tensor,
        nx: int,
        ny: Optional[int],
        start: Optional[int] = None,
        stop: Optional[int] = None,
        delay: Optional[int] = None,
        y_scale: float = 1.0,
    ):
        """
        :param start: In samples
        :param stop: In samples
        :param delay: In samples. Positive means we get rid of the start of x, end of y.
        """
        x, y = [z[start:stop] for z in (x, y)]
        if delay is not None:
            if delay > 0:
                x = x[:-delay]
                y = y[delay:]
            else:
                x = x[-delay:]
                y = y[:delay]
        y = y * y_scale
        self._validate_inputs(x, y, nx, ny)
        self._x = x
        self._y = y
        self._nx = nx
        self._ny = ny if ny is not None else len(x) - nx + 1

    def __getitem__(self, idx: int) -> Tuple[torch.Tensor, torch.Tensor]:
        if idx >= len(self):
            raise IndexError(f"Attempted to access datum {idx}, but len is {len(self)}")
        i = idx * self._ny
        j = i + self.y_offset
        return self.x[i : i + self._nx + self._ny - 1], self.y[j : j + self._ny]

    def __len__(self) -> int:
        n = len(self.x)
        # If ny were 1
        single_pairs = n - self._nx + 1
        return single_pairs // self._ny

    @property
    def x(self):
        return self._x

    @property
    def y(self):
        return self._y

    @property
    def y_offset(self) -> int:
        return self._nx - 1

    @classmethod
    def parse_config(cls, config):
        x, x_wavinfo = wav_to_tensor(config["x_path"], info=True)
        y = wav_to_tensor(
            config["y_path"],
            preroll=config.get("y_preroll"),
            required_shape=(len(x), 1),
            required_wavinfo=x_wavinfo,
        )
        return {
            "x": x,
            "y": y,
            "nx": config["nx"],
            "ny": config["ny"],
            "start": config.get("start"),
            "stop": config.get("stop"),
            "delay": config.get("delay"),
            "y_scale": config.get("y_scale", 1.0),
        }

    def _validate_inputs(self, x, y, nx, ny):
        assert x.ndim == 1
        assert y.ndim == 1
        assert len(x) == len(y)
        assert nx <= len(x)
        if ny is not None:
            assert ny <= len(y) - nx + 1


class ConcatDataset(AbstractDataset, InitializableFromConfig):
    def __init__(self, datasets: Sequence[Dataset]):
        self._datasets = datasets

    def __getitem__(self, idx: int) -> Tuple[torch.Tensor, torch.Tensor]:
        for d in self._datasets:
            if idx < len(d):
                return d[idx]
            else:
                idx = idx - len(d)

    def __len__(self) -> int:
        return sum(len(d) for d in self._datasets)

    @classmethod
    def parse_config(cls, config):
        return {"datasets": tuple(Dataset.init_from_config(c) for c in config)}


def init_dataset(config, split: Split) -> AbstractDataset:
    base_config = config[split.value]
    common = config.get("common", {})
    if isinstance(base_config, dict):
        return Dataset.init_from_config({**common, **base_config})
    elif isinstance(base_config, list):
        return ConcatDataset.init_from_config([{**common, **c} for c in base_config])
