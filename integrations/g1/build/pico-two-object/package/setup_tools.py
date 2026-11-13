#!/usr/bin/env python3
"""Fetch pinned upstream packaging tools locally; does not run SDK licence setup."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import urllib.request
import zipfile

HERE = Path(__file__).resolve().parent
TOOLS = HERE / "tools"
DOWNLOADS = [
    {
        "name": "apktool_3.0.3.jar",
        "url": "https://github.com/iBotPeaches/Apktool/releases/download/v3.0.3/apktool_3.0.3.jar",
        "algorithm": "sha256",
        "checksum": "dbf930b076c6b9be08d57c449cacefc3bdd6b71ebd59b3066fc0e1f5b14f9423",
        "checksum_source": "https://apktool.org/blog/apktool-3.0.3/",
    },
    {
        "name": "build-tools_r35.0.1_linux.zip",
        "url": "https://dl.google.com/android/repository/build-tools_r35.0.1_linux.zip",
        "algorithm": "sha1",
        "checksum": "e009a9b188cfeb1d2b4c318ab5cb4f1ddc368861",
        "checksum_source": "https://dl.google.com/android/repository/repository2-3.xml",
    },
]


def digest(path: Path, algorithm: str) -> str:
    h = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main() -> None:
    TOOLS.mkdir(parents=True, exist_ok=True)
    evidence = []
    for item in DOWNLOADS:
        path = TOOLS / item["name"]
        if not path.exists():
            temporary = path.with_suffix(path.suffix + ".download")
            request = urllib.request.Request(item["url"], headers={"User-Agent": "G1-Wrists-local-builder"})
            with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
                while block := response.read(1024 * 1024):
                    output.write(block)
            if digest(temporary, item["algorithm"]) != item["checksum"]:
                temporary.unlink()
                raise RuntimeError(f"Checksum mismatch: {item['name']}")
            temporary.replace(path)
        if digest(path, item["algorithm"]) != item["checksum"]:
            raise RuntimeError(f"Checksum mismatch: {path}; no existing tool was replaced")
        evidence.append({**item, "sha256": digest(path, "sha256"), "size": path.stat().st_size})
    with zipfile.ZipFile(TOOLS / "build-tools_r35.0.1_linux.zip") as source:
        for member in source.infolist():
            destination = (TOOLS / member.filename).resolve()
            if not destination.is_relative_to(TOOLS.resolve()):
                raise RuntimeError(f"Invalid archive path: {member.filename}")
            source.extract(member, TOOLS)
            mode = stat.S_IMODE(member.external_attr >> 16)
            if mode:
                destination.chmod(mode)
    (HERE / "tool-provenance.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print("Verified local apktool 3.0.3 and Android Build Tools 35.0.1.")


if __name__ == "__main__":
    main()
