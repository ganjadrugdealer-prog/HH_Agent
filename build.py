import os
import sys
import subprocess
from pathlib import Path

def main():
    print("=== starting cross-platform build ===")
    app_dir = Path(__file__).resolve().parent
    
    # Run pyinstaller directly if it is installed in the current env
    # For local development with venv: python -m PyInstaller
    print(f"Building in {app_dir}")
    
    spec_file = app_dir / "hh_agent.spec"
    
    # We use python -m PyInstaller
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(spec_file)]
    
    print("Running:", " ".join(cmd))
    res = subprocess.run(cmd, cwd=app_dir)
    
    if res.returncode != 0:
        print("Build failed.")
        sys.exit(res.returncode)
        
    print("=== result ===")
    dist_dir = app_dir / "dist"
    
    if sys.platform == "win32":
        target = dist_dir / "HH-Agent.exe"
    elif sys.platform == "darwin":
        target = dist_dir / "HH-Agent.app"
    else:
        target = dist_dir / "HH-Agent"

    if target.exists():
        if target.is_dir():
            print(f"App Bundle created successfully at {target}")
        else:
            print(f"SIZE_BYTES={target.stat().st_size}")
    else:
        print(f"Target not found: {target}")

    print("DONE")

if __name__ == "__main__":
    main()
