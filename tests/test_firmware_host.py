# ============================================================================
# Хостовый smoke-тест прошивочного IR-интерпретатора (firmware/ct_ir.h).
# Собираем ct_ir.h g++ с заглушками Arduino/ArduinoJson (tests/host_stubs/) и
# гоняем tests/firmware_smoke.cpp: парсинг IR-JSON, палитра, sync tick,
# честный Custom для неизвестного класса, watchdog-fallback, битый JSON.
# Это НЕ эмуляция ESP: проверяем переносимую логику заголовка. PlatformIO-
# сборку под реальные платы гоняем руками (см. firmware/README.md).
# Всё меняемое — в КОНФИБЛОКЕ ниже.
# ============================================================================
import shutil
import subprocess
from pathlib import Path

# ---- КОНФИГ ----------------------------------------------------------------
CXX = "g++"                       # компилятор; нет в PATH -> тест skip
STANDARD = "-std=c++17"           # как в современных platformio.ini
DEFINES = ["CT_HOST_TEST", "CT_NO_FASTLED"]   # хостовый режим без FastLED
TIMEOUT_S = 60                    # лимит сборки+прогона
# -----------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "tests" / "firmware_smoke.cpp"


def test_ct_ir_host_smoke(tmp_path):
    if shutil.which(CXX) is None:
        pytest_skip(f"{CXX} not found")
    flags = [STANDARD] + [f"-D{d}" for d in DEFINES] + \
            ["-I", str(ROOT / "tests" / "host_stubs"), "-I", str(ROOT)]
    exe = tmp_path / "smoke"
    build = subprocess.run([CXX, *flags, str(SRC), "-o", str(exe)],
                           capture_output=True, text=True, timeout=TIMEOUT_S)
    assert build.returncode == 0, f"compile failed:\n{build.stderr}"
    run = subprocess.run([str(exe)], capture_output=True, text=True,
                         timeout=TIMEOUT_S)
    assert run.returncode == 0, f"smoke failed:\n{run.stdout}{run.stderr}"
    assert "SMOKE OK" in run.stdout


def pytest_skip(msg):
    import pytest
    pytest.skip(msg)
