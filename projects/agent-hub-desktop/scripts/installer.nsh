!macro customInstall
  WriteINIStr "$INSTDIR\管理后台.url" "InternetShortcut" "URL" "https://47.109.23.86:18180/"
  WriteINIStr "$INSTDIR\管理后台.url" "InternetShortcut" "IconFile" "$INSTDIR\Agent Hub.exe"
  WriteINIStr "$INSTDIR\管理后台.url" "InternetShortcut" "IconIndex" "0"
  CopyFiles /SILENT "$INSTDIR\管理后台.url" "$DESKTOP\Agent Hub 管理后台.url"
  CopyFiles /SILENT "$INSTDIR\管理后台.url" "$SMPROGRAMS\Agent Hub 管理后台.url"
!macroend

!macro customUnInstall
  Delete "$DESKTOP\Agent Hub 管理后台.url"
  Delete "$SMPROGRAMS\Agent Hub 管理后台.url"
  Delete "$INSTDIR\管理后台.url"
  ReadRegStr $0 HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Agent Hub"
  ${If} $0 == '"$INSTDIR\Agent Hub.exe" --hidden'
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Agent Hub"
  ${EndIf}
!macroend
