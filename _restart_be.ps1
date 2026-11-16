Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'topwar_web' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 1
Start-Process -WindowStyle Hidden python -ArgumentList 'topwar_web.py' -WorkingDirectory 'g:\code\py\pdfReader\seasoilder'
Start-Sleep -Seconds 2
(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8765/api/status).Content
(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8765/api/queues).Content
