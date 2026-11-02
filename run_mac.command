#!/bin/bash
# LedsGirls launcher for macOS
cd "$(dirname "$0")" || exit 1

echo "===================================="
echo "          ORBIT CRM"
echo "===================================="
echo ""

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 не найден."
  echo "Установите Python 3 с https://www.python.org/downloads/macos/"
  echo ""
  read -r -p "Нажмите Enter для закрытия..."
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "Создаю виртуальное окружение..."
  python3 -m venv .venv || {
    echo "Не удалось создать окружение Python."
    read -r -p "Нажмите Enter для закрытия..."
    exit 1
  }
fi

source .venv/bin/activate

echo "Устанавливаю необходимые библиотеки..."
python -m pip install --upgrade pip || exit 1
python -m pip install -r requirements.txt || {
  echo "Не удалось установить зависимости. Проверьте интернет-соединение."
  read -r -p "Нажмите Enter для закрытия..."
  exit 1
}

echo ""
echo "Проверяю поддержку хеширования паролей..."
python - <<'PY'
from werkzeug.security import generate_password_hash, check_password_hash
hashed = generate_password_hash("orbit-check", method="pbkdf2:sha256", salt_length=16)
assert check_password_hash(hashed, "orbit-check")
print("Проверка пройдена.")
PY

echo ""
echo "CRM запускается: http://127.0.0.1:5000"
echo "Не закрывайте это окно Terminal, пока пользуетесь CRM."
echo "Для остановки нажмите Ctrl+C."
echo ""
python app.py
status=$?
echo ""
echo "LedsGirls остановлена (код: $status)."
read -r -p "Нажмите Enter для закрытия..."
exit "$status"
