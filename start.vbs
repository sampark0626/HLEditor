' HLEditor 조용한 실행 (콘솔 창 없음)
' 이 파일을 더블클릭하면 앱이 시작되고 브라우저가 자동으로 열립니다.
Set fso = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")

' 이 스크립트가 있는 폴더를 작업 디렉토리로 설정
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = scriptDir

' python app.py 를 숨김 모드(0)로 실행
' 주의: 'python'은 이 PC에서 Microsoft Store 스텁으로 해석되어 실행되지 않으므로
'       의존성이 설치된 실제 파이썬의 전체 경로를 직접 지정한다.
WshShell.Run """C:\Users\SKTelecom\AppData\Local\Python\bin\python.exe"" app.py", 0, False
