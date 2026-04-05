Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "D:\workdir\xiaolong-openclaw"
WshShell.Run "cmd /c start.bat", 1, False
