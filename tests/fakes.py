"""Заглушки внешних систем для юнит-тестов: Photos (FakeLibrary) и Claude API (FakeClient)."""

import json
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2

from photo_chooser.library.base import DownloadError, resolve_album
from photo_chooser.models import AlbumRef, Asset


class FakeLibrary:
    """PhotosLibrary на файлах: previews/files/live — пути по uuid, failing — uuid, оригинал которых не скачивается."""

    def __init__(self, albums: list[AlbumRef], assets: dict[str, list[Asset]], previews: dict[str, Path],
                 files: dict[str, Path] | None = None, live: dict[str, Path] | None = None,
                 failing: set[str] | None = None):
        self._albums = albums
        self._assets = assets
        self._previews = previews
        self._files = files or {}
        self._live = live or {}
        self._failing = failing or set()
        self.exported: list[str] = []
        self.live_requests: list[str] = []
        self.preview_requests: list[str] = []

    def add_album(self, album: AlbumRef, assets: list[Asset], previews: dict[str, Path]) -> None:
        self._albums.append(album)
        self._assets[album.uuid] = list(assets)
        self._previews.update(previews)

    def albums(self) -> list[AlbumRef]:
        return list(self._albums)

    def find_album(self, title: str) -> AlbumRef:
        return resolve_album(title, self._albums)

    def assets(self, album: AlbumRef, *, album_order: bool = False) -> list[Asset]:
        # в фейке список и есть порядок альбома; сортировку по дате делает вызывающий код
        return list(self._assets.get(album.uuid, []))

    def preview(self, asset: Asset, long_side: int) -> Path | None:
        self.preview_requests.append(asset.uuid)
        return self._previews.get(asset.uuid)

    def export_original(self, asset: Asset, dest_dir: Path) -> Path:
        if asset.uuid in self._failing or asset.uuid not in self._files:
            raise DownloadError(asset.uuid, "3 попытки не удались: network")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / asset.filename
        shutil.copyfile(self._files[asset.uuid], dest)
        self.exported.append(asset.uuid)
        return dest

    def export_live_video(self, asset: Asset, dest_dir: Path) -> Path:
        self.live_requests.append(asset.uuid)
        if asset.uuid not in self._live:
            raise DownloadError(asset.uuid, "3 попытки не удались: network")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / (Path(asset.filename).stem + ".MOV")
        shutil.copyfile(self._live[asset.uuid], dest)
        return dest


# --- Claude API -------------------------------------------------------------------------------

def rate_limit_error() -> anthropic.RateLimitError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.RateLimitError("rate limited", response=httpx2.Response(429, request=request), body=None)


def api_error() -> anthropic.APIStatusError:
    """Ошибка API, которую не повторяют (400): так же ведёт себя ошибка, оставшаяся после всех повторов."""
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.BadRequestError("invalid request", response=httpx2.Response(400, request=request), body=None)


def server_error(status: int = 500) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.InternalServerError("overloaded", response=httpx2.Response(status, request=request), body=None)


def timeout_error() -> anthropic.APITimeoutError:
    return anthropic.APITimeoutError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))


def connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))


def credit_error() -> anthropic.BadRequestError:
    """Так API отвечает при пустом балансе (прогон 2026-11-03_0114): 400, а не 402."""
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.BadRequestError("Your credit balance is too low to access the Anthropic API. Please go to Plans & "
                                     "Billing to upgrade or purchase credits.",
                                     response=httpx2.Response(400, request=request), body=None)


def billing_error() -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIStatusError("billing", response=httpx2.Response(402, request=request),
                                    body={"type": "error", "error": {"type": "billing_error", "message": "billing"}})


def auth_error() -> anthropic.AuthenticationError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.AuthenticationError("invalid x-api-key", response=httpx2.Response(401, request=request), body=None)


def fake_response(parsed=None, *, text=None, stop_reason="end_turn", input_tokens=20_000, output_tokens=2_000,
                  cache_write=0, cache_read=0):
    """Ответ messages.create: блок thinking (как у моделей с adaptive thinking) и текст с JSON по схеме."""
    body = text if text is not None else (parsed.model_dump_json() if parsed is not None else None)
    content = [SimpleNamespace(type="thinking", thinking="")]
    if body is not None:
        content.append(SimpleNamespace(type="text", text=body))
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                            cache_creation_input_tokens=cache_write, cache_read_input_tokens=cache_read)
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage)


def output_model_of(kwargs: dict):
    """Какая ступень запрошена — по JSON-схеме в output_config.format."""
    from photo_chooser.judge.stages import ComposeOutput, JudgeOutput, ScreenOutput

    schema = json.dumps(kwargs["output_config"]["format"]["schema"])
    if "carousels" in schema:
        return ComposeOutput
    return JudgeOutput if "matches_theme" in schema else ScreenOutput


def ids_in(content: list[dict]) -> list[str]:
    # подпись второй ступени — «id: p01 (horizontal)»: id — первое слово после «id: »
    return [b["text"].removeprefix("id: ").split(" ")[0] for b in content
            if b["type"] == "text" and b["text"].startswith("id: ")]


def last_text(content: list[dict]) -> str:
    return [b["text"] for b in content if b["type"] == "text"][-1]


def _quiet(message: str) -> None:
    pass


def send(client, req, notify=None):
    """Один запрос синхронно, как его отправляет SyncTransport."""
    from photo_chooser.judge.transport import call_model

    return call_model(client, req, notify or _quiet)


def screen_group(client, runner, album_title, themes, group, notify=None):
    return runner.parse(send(client, runner.request(album_title, themes, group, "s-001"), notify), themes,
                        [a.uuid for a in group])


def judge_group(client, runner, theme, group, notify=None):
    from photo_chooser.judge.stages import orientation

    return runner.parse(send(client, runner.request(theme, group, "j-test-01"), notify),
                        [(a.uuid, orientation(a)) for a in group])


def compose_once(client, runner, theme, cands, facts, limits):
    return runner.parse(send(client, runner.request(theme, cands, facts, limits, "c-test")))


def screen_run(client, runner, cache, album_title, album_uuid, themes, assets, cfg, log=None):
    from photo_chooser.judge.stages import run_screen
    from photo_chooser.judge.transport import SyncTransport

    return run_screen(runner, SyncTransport(client), cache, album_title, album_uuid, themes, assets, cfg, log or _quiet)


def judge_one(client, runner, cache, theme, assets, order, cfg, album_uuid, log=None):
    """Вторая ступень одной темы через синхронный транспорт; исход этой темы."""
    from photo_chooser.judge.stages import JudgeTask, run_judge
    from photo_chooser.judge.transport import SyncTransport

    return run_judge(runner, SyncTransport(client), cache, [JudgeTask(theme, assets, order)], cfg, album_uuid,
                     log or _quiet)[theme.key()]


def compose_one(client, runner, cache, theme, cands, facts, cfg, album_uuid, log=None):
    from photo_chooser.judge.stages import ComposeTask, run_compose
    from photo_chooser.judge.transport import SyncTransport

    return run_compose(runner, SyncTransport(client), cache, [ComposeTask(theme, cands, facts)], cfg, album_uuid,
                       log or _quiet)[theme.key()]


class _Messages:
    def __init__(self, owner: "FakeClient", beta: bool):
        self._owner, self._beta = owner, beta

    def create(self, **kwargs):
        return self._owner._call(self._beta, kwargs)


class FakeClient:
    """Заглушка anthropic.Anthropic: handler(n, kwargs) → ответ или исключение; n — номер вызова с 1.

    calls — kwargs каждого вызова (с ключом "_beta"), в порядке поступления.
    """

    def __init__(self, handler):
        self._handler = handler
        self._lock = threading.Lock()
        self.calls: list[dict] = []
        self.messages = _Messages(self, beta=False)
        self.beta = SimpleNamespace(messages=_Messages(self, beta=True))

    def _call(self, beta: bool, kwargs: dict):
        with self._lock:
            self.calls.append({**kwargs, "_beta": beta})
            n = len(self.calls)
        result = self._handler(n, kwargs)
        if isinstance(result, BaseException):
            raise result
        return result

    def calls_for(self, output_model) -> list[dict]:
        return [c for c in self.calls if output_model_of(c) is output_model]
