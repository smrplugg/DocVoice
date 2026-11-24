; Установщик DocVoice (Inno Setup 6).
; Собирается автоматически на GitHub (.github/workflows/build.yml), вручную:
;   iscc /DAppVersion=1.2.0 /DSourceDir=..\dist\DocVoice installer\DocVoice.iss
;
; Рассчитан на неопытных людей: ставится без прав администратора и без вопросов
; «куда ставить», сам создаёт ярлык на рабочем столе и в конце запускает программу.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\DocVoice"
#endif
#ifndef OutputDir
  #define OutputDir "..\release"
#endif

#define AppName "DocVoice"
#define AppExe "DocVoice.exe"

[Setup]
; постоянный идентификатор — по нему новая версия ставится поверх старой
AppId={{6F1C2E7A-4B8D-4C1E-9A57-D0C5E1B7A3F2}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=smrplugg
AppPublisherURL=https://github.com/smrplugg/DocVoice
AppSupportURL=https://github.com/smrplugg/DocVoice/issues
AppComments=Читает книги и документы вслух, без интернета

; ставим в профиль пользователя: не нужен пароль администратора,
; и программа может сама писать в свою папку data (голоса, настройки)
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#AppName}
DisableWelcomePage=no
DisableDirPage=yes
DisableProgramGroupPage=yes
DefaultGroupName={#AppName}
UsePreviousAppDir=yes

OutputDir={#OutputDir}
OutputBaseFilename=DocVoice-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=4
WizardStyle=modern
WizardSizePercent=120
ShowLanguageDialog=no
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName} — чтение книг вслух
; закрыть запущенную программу при обновлении
CloseApplications=yes
RestartApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
#ifexist "DocVoice.ico"
SetupIconFile=DocVoice.ico
#endif

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Messages]
ru.WelcomeLabel2=Программа [name/ver] будет установлена на этот компьютер.%n%nОна читает вслух книги и документы и умеет записывать их в MP3.%n%nНажмите «Далее».
ru.FinishedLabel=Готово! Значок DocVoice появился на рабочем столе.%n%nЧтобы открыть программу позже, дважды щёлкните по нему.

[Files]
; программа целиком, кроме стартовых настроек (они ниже — отдельно)
Source: "{#SourceDir}\*"; DestDir: "{app}"; Excludes: "\data\settings.default.json,\data\logs"; Flags: ignoreversion recursesubdirs createallsubdirs
; стартовые настройки (Silero, голос Ксения) — только при первой установке,
; при обновлении настройки человека не трогаем
Source: "{#SourceDir}\data\settings.default.json"; DestDir: "{app}\data"; DestName: "settings.json"; Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Comment: "Читать книги вслух"
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Инструкция"; Filename: "{app}\Инструкция.txt"
Name: "{group}\Полное руководство"; Filename: "{app}\Руководство.txt"
Name: "{group}\Удалить {#AppName}"; Filename: "{uninstallexe}"

[Registry]
; DocVoice появляется в меню «Открыть с помощью» для книг и документов.
; Программу по умолчанию для PDF/Word это НЕ меняет.
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "DocVoice — прочитать вслух"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\shell\open\command"; ValueType: string; ValueData: """{app}\{#AppExe}"" ""%1"""
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".pdf"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".docx"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".doc"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".odt"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".rtf"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".txt"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".epub"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".fb2"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".djvu"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".pptx"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\.pdf\OpenWithProgids"; ValueType: none; ValueName: "Applications\{#AppExe}"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.docx\OpenWithProgids"; ValueType: none; ValueName: "Applications\{#AppExe}"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.doc\OpenWithProgids"; ValueType: none; ValueName: "Applications\{#AppExe}"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.epub\OpenWithProgids"; ValueType: none; ValueName: "Applications\{#AppExe}"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.fb2\OpenWithProgids"; ValueType: none; ValueName: "Applications\{#AppExe}"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.txt\OpenWithProgids"; ValueType: none; ValueName: "Applications\{#AppExe}"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "Открыть DocVoice"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; журналы и временные файлы, которые программа создала сама
Type: filesandordirs; Name: "{app}\data\logs"
Type: files; Name: "{app}\data\*.tmp"
