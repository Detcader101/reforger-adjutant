# List Windows python processes running the bot, with their command lines.
Get-CimInstance Win32_Process -Filter "name='python.exe'" |
    Select-Object ProcessId, CommandLine |
    Format-Table -AutoSize -Wrap
