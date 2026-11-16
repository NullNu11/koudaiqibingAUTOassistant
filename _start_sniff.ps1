$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
Start-Process -WindowStyle Minimized -FilePath python -ArgumentList "-X","utf8","-u","topwar_sniff.py" -WorkingDirectory $dir -RedirectStandardOutput "$dir\_sniff_out.log" -RedirectStandardError "$dir\_sniff_err.log"
