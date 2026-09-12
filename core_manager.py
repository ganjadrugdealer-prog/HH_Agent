import os
import sys
import json
import urllib.request
import zipfile
import tarfile
import shutil
from pathlib import Path

# Внимание: замените YOUR-USERNAME/YOUR-REPO на ваш реальный репозиторий GitHub.
# Этот файл должен лежать в корне ветки main.
REMOTE_CONFIG_URL = "https://raw.githubusercontent.com/ganjadrugdealer-prog/HH_Agent/main/remote_config.json"

def get_core_path() -> Path:
    if sys.platform == "win32":
        base_dir = Path(os.environ.get("APPDATA", os.path.expanduser("~")))
    elif sys.platform == "darwin":
        base_dir = Path(os.path.expanduser("~/Library/Application Support"))
    else:
        base_dir = Path(os.path.expanduser("~/.config"))
    
    return base_dir / "HH_Agent" / "core"

def get_installed_version() -> str | None:
    version_file = get_core_path() / "version.txt"
    if version_file.exists():
        try:
            return version_file.read_text(encoding="utf-8").strip()
        except Exception:
            return None
    return None

def is_core_installed() -> bool:
    core_path = get_core_path()
    if not core_path.exists():
        return False
    return (core_path / "hh_applicant_tool").exists() or (core_path / "hh_applicant_tool.py").exists()

def get_approved_version() -> str | None:
    """Пытается получить одобренную версию с GitHub. В случае неудачи возвращает None."""
    try:
        req = urllib.request.Request(REMOTE_CONFIG_URL)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            return data.get("approved_core_version")
    except Exception as e:
        # Если ссылка не настроена или нет интернета
        print(f"Failed to fetch remote config: {e}")
        return None

def install_core(progress_callback=None, target_version=None):
    core_path = get_core_path()
    
    if target_version is None:
        if progress_callback:
            progress_callback("Проверка удаленного манифеста (GitHub)...")
        target_version = get_approved_version()

    if progress_callback:
        progress_callback("Получение информации из PyPI...")
        
    pypi_url = "https://pypi.org/pypi/hh-applicant-tool/json"
    req = urllib.request.Request(pypi_url)
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
        
    if not target_version:
        target_version = data["info"]["version"]
        
    if target_version not in data["releases"]:
        raise Exception(f"Version {target_version} not found on PyPI.")
        
    urls = data["releases"][target_version]
    
    # Prefer wheel, fallback to tar.gz
    download_url = None
    is_wheel = False
    for u in urls:
        if u["filename"].endswith(".whl"):
            download_url = u["url"]
            is_wheel = True
            break
    
    if not download_url:
        for u in urls:
            if u["filename"].endswith(".tar.gz"):
                download_url = u["url"]
                break
                
    if not download_url:
        raise Exception("Could not find suitable release artifact on PyPI.")
        
    if progress_callback:
        progress_callback(f"Скачивание ядра v{target_version}...")
        
    if core_path.exists():
        shutil.rmtree(core_path, ignore_errors=True)
    core_path.mkdir(parents=True, exist_ok=True)

    archive_path = core_path / "downloaded_archive"
    urllib.request.urlretrieve(download_url, archive_path)
    
    if progress_callback:
        progress_callback("Распаковка...")
        
    try:
        if is_wheel:
            with zipfile.ZipFile(archive_path, 'r') as z:
                z.extractall(core_path)
        else:
            with tarfile.open(archive_path, 'r:gz') as t:
                for member in t.getmembers():
                    # To avoid nested folders like hh-applicant-tool-1.2.3/hh_applicant_tool
                    # Prevent directory traversal attacks
                    if '..' in member.name or member.name.startswith('/'):
                        continue
                        
                    parts = Path(member.name).parts
                    if len(parts) > 1:
                        member.name = str(Path(*parts[1:]))
                        t.extract(member, path=core_path)
                        
        # Сохраняем версию для будущих проверок
        (core_path / "version.txt").write_text(target_version, encoding="utf-8")
    finally:
        if archive_path.exists():
            archive_path.unlink()
            
    if progress_callback:
        progress_callback("Ядро успешно установлено!")

def init_core():
    """Injects core path to sys.path so 'hh_applicant_tool' is importable"""
    core_path = get_core_path()
    path_str = str(core_path.resolve())
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
