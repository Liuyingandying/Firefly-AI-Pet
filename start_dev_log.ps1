$dir = 'C:\Users\LENOVO\Desktop\流萤\Firefly-AI-Pet（2）'
Set-Location $dir
Start-Process -FilePath '.venv\Scripts\pythonw.exe' -ArgumentList 'app.py' -WorkingDirectory $dir -RedirectStandardError 'ask_err.log' -RedirectStandardOutput 'ask_out.log'
Write-Host 'started'
