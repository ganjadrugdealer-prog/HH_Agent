# -*- coding: utf-8 -*-
"""Установка и подключение консольного ядра hh-applicant-tool.

Ядро не входит в сборку: приложение скачивает его с PyPI при первом запуске
и кладёт в пользовательскую папку. Версия берётся из remote_config.json в
репозитории — так можно откатить всех пользователей на рабочую версию ядра,
не пересобирая exe.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import ssl
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REMOTE_CONFIG_URL = (
    "https://raw.githubusercontent.com/ganjadrugdealer-prog/HH_Agent"
    "/main/remote_config.json"
)
PYPI_URL = "https://pypi.org/pypi/hh-applicant-tool/json"
PACKAGE_DIR = "hh_applicant_tool"
# Версия ядра, под которую написано приложение (= engine.ENGINE_EXPECTED).
# Ставится, если remote_config.json недоступен. Брать «последнюю с PyPI» нельзя:
# с 2.x движок несовместим, а raw.githubusercontent.com из РФ часто не отвечает.
FALLBACK_CORE_VERSION = "1.8.28"
REINSTALL_MARKER = "reinstall.flag"
# sha256 архивов проверенных версий (как на PyPI). По ним принимается ядро,
# скачанное пользователем в браузере: в корпоративной сети программе бывает
# закрыт PyPI, а браузеру — нет. Чужой или испорченный файл не ставится.
KNOWN_ARCHIVES = {
    "91aafd14d8885261983ba5aaee58a484383e39b37d6e253ca8ce1187af83c5ef": ("1.8.28", True),
    "59f2aa2f793d3f0f457b6945e947e8cfc60413a0c01817a01f8e09528258ff26": ("1.8.28", False),
}

log = logging.getLogger("hh_agent.loader.core")
log.addHandler(logging.NullHandler())


def ssl_context() -> ssl.SSLContext:
    """SSL-контекст с корневыми сертификатами certifi.

    Python в собранном .app на macOS не видит системную связку ключей, и
    urllib без явного контекста падает с CERTIFICATE_VERIFY_FAILED.
    Если certifi недоступен — системные настройки по умолчанию.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _is_cert_error(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, ssl.SSLCertVerificationError)


def _urlopen(req: urllib.request.Request, timeout: float, context: ssl.SSLContext):
    return urllib.request.urlopen(req, timeout=timeout, context=context)


def _open(req: urllib.request.Request, timeout: float):
    """Запрос с проверкой сертификата: сначала certifi, затем системные.

    Корпоративные сети с проверкой HTTPS подменяют сертификат своим корневым.
    В certifi его нет, а в хранилище Windows он стоит. Проверка сертификата
    не отключается: повтор доверяет тому же, чему доверяет браузер.
    """
    try:
        return _urlopen(req, timeout, ssl_context())
    except (urllib.error.URLError, ssl.SSLError) as exc:
        if not _is_cert_error(exc):
            raise
        log.warning("сертификат %s не принят certifi (%s), повтор с системными",
                    req.full_url, exc)
        return _urlopen(req, timeout, ssl.create_default_context())


def app_data_path() -> Path:
    if sys.platform == "win32":
        base_dir = Path(os.environ.get("APPDATA", os.path.expanduser("~")))
    elif sys.platform == "darwin":
        base_dir = Path(os.path.expanduser("~/Library/Application Support"))
    else:
        base_dir = Path(os.path.expanduser("~/.config"))
    return base_dir / "HH_Agent"


def get_core_path() -> Path:
    return app_data_path() / "core"


def marker_path() -> Path:
    return app_data_path() / REINSTALL_MARKER


def request_reinstall() -> None:
    """Пометить ядро к переустановке при следующем запуске.

    Удалять папку ядра на ходу не стоит: её модули уже загружены, а
    неполное удаление оставит битое ядро, которое is_core_installed()
    сочтёт исправным. Метку видит следующий запуск до импорта ядра.
    """
    p = marker_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("reinstall", encoding="utf-8")


def clear_reinstall() -> None:
    try:
        marker_path().unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def get_installed_version() -> str | None:
    version_file = get_core_path() / "version.txt"
    if version_file.exists():
        try:
            return version_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return None


def is_core_installed() -> bool:
    if marker_path().exists():
        return False
    core_path = get_core_path()
    return (core_path / PACKAGE_DIR / "__init__.py").exists()


def get_approved_version() -> str | None:
    """Одобренная версия ядра из репозитория. None — если недоступна."""
    try:
        req = urllib.request.Request(
            REMOTE_CONFIG_URL, headers={"User-Agent": "HH-Agent"}
        )
        with _open(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        version = data.get("approved_core_version")
        return str(version) if version else None
    except Exception:
        return None


def _fetch_pypi() -> dict:
    req = urllib.request.Request(PYPI_URL, headers={"User-Agent": "HH-Agent"})
    with _open(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _windows_downloads() -> Path | None:
    """Настоящая папка «Загрузки»: на корпоративных ПК её переносят в OneDrive."""
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD),
                        ("d3", wintypes.WORD), ("d4", ctypes.c_ubyte * 8)]

        folder = GUID(0x374DE290, 0x123F, 0x4565,
                      (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
        buf = ctypes.c_wchar_p()
        shell32 = ctypes.windll.shell32
        if shell32.SHGetKnownFolderPath(ctypes.byref(folder), 0, None, ctypes.byref(buf)):
            return None
        try:
            return Path(buf.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(buf)
    except Exception:
        return None


def local_archive_dirs() -> list[Path]:
    """Где искать ядро, скачанное вручную: «Загрузки», папка программы, папка данных."""
    dirs = [Path.home() / "Downloads"]
    if sys.platform == "win32":
        known = _windows_downloads()
        if known and known not in dirs:
            dirs.insert(0, known)
    if getattr(sys, "frozen", False) and sys.platform != "darwin":
        dirs.append(Path(sys.executable).resolve().parent)
    dirs.append(app_data_path())
    return dirs


def find_local_archive(version: str) -> tuple[Path, bool] | None:
    """Проверенный архив ядра нужной версии, если пользователь его положил.

    Имя может быть с припиской браузера («... (1).whl») — решает sha256.
    """
    for d in local_archive_dirs():
        try:
            candidates = sorted(d.glob(f"hh_applicant_tool-{version}*"))
        except OSError:
            continue
        for p in candidates:
            if not p.is_file():
                continue
            try:
                known = KNOWN_ARCHIVES.get(_sha256(p))
            except OSError as exc:
                log.warning("не читается %s: %s", p, exc)
                continue
            if known and known[0] == version:
                return p, known[1]
            log.warning("%s не совпадает с архивом %s на PyPI — пропущен", p, version)
    return None


def manual_install_hint(version: str) -> str:
    """Как поставить ядро руками, если программе закрыт PyPI. Пусто — не умеем."""
    if not any(v == version and wheel for v, wheel in KNOWN_ARCHIVES.values()):
        return ""
    return (
        "Если интернет работает (например, корпоративная сеть), скачайте "
        f"в браузере файл hh_applicant_tool-{version}-py3-none-any.whl "
        f"со страницы https://pypi.org/project/hh-applicant-tool/{version}/#files "
        "в папку «Загрузки» или рядом с программой и нажмите «Повторить»."
    )


def _pick_artifact(files: list[dict]) -> tuple[str, bool]:
    for f in files:
        if f.get("filename", "").endswith(".whl"):
            return f["url"], True
    for f in files:
        if f.get("filename", "").endswith(".tar.gz"):
            return f["url"], False
    raise RuntimeError("На PyPI нет подходящего архива с ядром.")


def _safe_members(tar: tarfile.TarFile, dest: Path):
    """Члены архива без первого каталога и без выхода за пределы dest."""
    dest = dest.resolve()
    for member in tar.getmembers():
        name = member.name.replace("\\", "/")
        parts = [p for p in Path(name).parts if p not in ("", ".")]
        if any(p == ".." for p in parts) or name.startswith("/"):
            continue
        if len(parts) > 1:
            parts = parts[1:]          # срезаем pkg-1.2.3/
        elif member.isdir():
            continue                    # сам корневой каталог не нужен
        member.name = str(Path(*parts))
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest)):
            continue
        yield member


def _extract_sdist(tar: tarfile.TarFile, dest: Path) -> None:
    """Распаковка sdist без первого каталога.

    Логика та же, что была, плюс три вещи, которых не хватало: явная
    проверка, что распакованный путь не вылезает за dest; пропуск
    симлинков и спецфайлов; и распаковка через extractfile вместо
    tar.extract — в Python 3.14 у extract меняется поведение по умолчанию.
    """
    for member in _safe_members(tar, dest):
        if member.isdir():
            (dest / member.name).mkdir(parents=True, exist_ok=True)
            continue
        if not (member.isfile() or member.isreg()):
            continue                    # симлинки и устройства не распаковываем
        target = dest / member.name
        target.parent.mkdir(parents=True, exist_ok=True)
        src = tar.extractfile(member)
        if src is None:
            continue
        with src, open(target, "wb") as fp:
            shutil.copyfileobj(src, fp)


def _extract_wheel(archive: Path, dest: Path) -> None:
    with zipfile.ZipFile(archive, "r") as z:
        dest_res = dest.resolve()
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts:
                continue
            target = (dest / name).resolve()
            if not str(target).startswith(str(dest_res)):
                continue
            z.extract(info, dest)


def _rmtree_retry(path: Path, attempts: int = 5) -> None:
    for i in range(attempts):
        if not path.exists():
            return
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.4 * (i + 1))
    if path.exists():
        raise RuntimeError(
            f"Не удалось удалить старое ядро: {path}. Закройте приложение и повторите."
        )


def install_core(progress_callback=None, target_version: str | None = None) -> str:
    """Скачивает и распаковывает ядро. Возвращает установленную версию."""

    def say(msg: str) -> None:
        if progress_callback:
            try:
                progress_callback(msg)
            except Exception:
                pass

    core_path = get_core_path()
    staging = core_path.with_name("core.new")

    if target_version is None:
        say("Проверка удалённого манифеста (GitHub)...")
        target_version = get_approved_version()
    if not target_version:
        say(f"Манифест недоступен, ставлю проверенную версию {FALLBACK_CORE_VERSION}")
        target_version = FALLBACK_CORE_VERSION
    log.info("ставится ядро %s", target_version)

    _rmtree_retry(staging)
    staging.mkdir(parents=True, exist_ok=True)

    local = find_local_archive(target_version)
    if local:
        log.info("ядро из файла %s", local[0])
        say(f"Ядро v{target_version} найдено в {local[0].name}, распаковка...")
        _unpack(local[0], local[1], staging)
    else:
        try:
            _download_and_unpack(target_version, staging, say)
        except OSError as exc:
            # URLError, таймаут, SSL — всё OSError: сети нет или она закрыта.
            log.exception("ядро не скачалось")
            hint = manual_install_hint(target_version)
            raise RuntimeError(f"{exc}\n\n{hint}" if hint else str(exc)) from exc

    if not (staging / PACKAGE_DIR / "__init__.py").exists():
        _rmtree_retry(staging)
        raise RuntimeError(
            "В архиве не оказалось пакета hh_applicant_tool — установка отменена."
        )

    (staging / "version.txt").write_text(target_version, encoding="utf-8")

    say("Подключение ядра...")
    _rmtree_retry(core_path)
    staging.replace(core_path)
    clear_reinstall()

    say("Ядро успешно установлено!")
    return target_version


def _unpack(archive: Path, is_wheel: bool, staging: Path) -> None:
    if is_wheel:
        _extract_wheel(archive, staging)
    else:
        with tarfile.open(archive, "r:gz") as t:
            _extract_sdist(t, staging)


def _download_and_unpack(version: str, staging: Path, say) -> None:
    say("Получение информации из PyPI...")
    data = _fetch_pypi()
    if version not in data.get("releases", {}):
        raise RuntimeError(f"Версия {version} не найдена на PyPI.")

    download_url, is_wheel = _pick_artifact(data["releases"][version])

    say(f"Скачивание ядра v{version}...")
    archive_path = staging / "downloaded_archive"
    try:
        req = urllib.request.Request(
            download_url, headers={"User-Agent": "HH-Agent"}
        )
        with _open(req, timeout=60) as resp, \
                open(archive_path, "wb") as fp:
            shutil.copyfileobj(resp, fp)

        say("Распаковка...")
        _unpack(archive_path, is_wheel, staging)
    finally:
        if archive_path.exists():
            try:
                archive_path.unlink()
            except OSError:
                pass


def init_core() -> None:
    """Добавляет папку ядра в sys.path, чтобы hh_applicant_tool импортировался."""
    core_path = get_core_path()
    path_str = str(core_path.resolve())
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
