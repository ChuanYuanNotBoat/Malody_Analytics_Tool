@echo off
echo Installing dependencies...
pip install -r requirements.txt

echo Setup completed. You can now run the application with:
echo python main.py
echo.
echo Optional test run:
echo python -m unittest discover -s tests -p "test_*.py" -v
echo.
echo Optional Windows build:
echo powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
pause
