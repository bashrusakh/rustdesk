#include <windows.h>
//
#include <cstdint>
#include <iostream>
#include <psapi.h>
#include <sddl.h>
#include <tlhelp32.h>

extern "C" int64_t FindProcessPid(LPCWSTR exename, int verbose) {
    PROCESSENTRY32 p32;
    p32.dwSize = sizeof(PROCESSENTRY32);

    int64_t processWinlogonPid = -1;

    HANDLE hSnapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (hSnapshot == INVALID_HANDLE_VALUE) {
        if (verbose)
            std::cout << "[!] Failed to create snapshot" << std::endl;
        goto _exit;
    }

    if (Process32First(hSnapshot, &p32)) {
        do {
            if (wcscmp(p32.szExeFile, exename) == 0) {
                processWinlogonPid = p32.th32ProcessID;
                break;
            }
        } while (Process32Next(hSnapshot, &p32));
    }
_exit:
    if (verbose && processWinlogonPid < 0)
        std::cout << "[!] Failed to find pid of " << exename << std::endl;
    if (hSnapshot != INVALID_HANDLE_VALUE)
        CloseHandle(hSnapshot);

    return processWinlogonPid;
}

static int EnableSeDebugPrivilegePrivilege() {
    LUID luid;
    HANDLE currentProc =
        OpenProcess(PROCESS_ALL_ACCESS, false, GetCurrentProcessId());
    int ret = -1;

    if (currentProc) {
        HANDLE TokenHandle(NULL);
        if (OpenProcessToken(currentProc, TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                             &TokenHandle)) {
            // https://learn.microsoft.com/en-us/windows/win32/secauthz/enabling-and-disabling-privileges-in-c--
            if (!LookupPrivilegeValue(NULL, L"SeDebugPrivilege", &luid)) {
                std::cout << "[!] Failed to lookup privilege on local system"
                          << std::endl;
            } else {
                TOKEN_PRIVILEGES tokenPrivs;

                tokenPrivs.PrivilegeCount = 1;
                tokenPrivs.Privileges[0].Luid = luid;
                tokenPrivs.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED;

                if (AdjustTokenPrivileges(TokenHandle, FALSE, &tokenPrivs,
                                          sizeof(TOKEN_PRIVILEGES),
                                          (PTOKEN_PRIVILEGES)NULL,
                                          (PDWORD)NULL)) {
                    ret = 0;
                } else {
                    std::cout << "[!] Failed to add SeDebugPrivilege to the "
                                 "current process token"
                              << std::endl;
                }

                if (GetLastError() == ERROR_NOT_ALL_ASSIGNED) {
                    std::cout << "[!] The token does not have the specified "
                                 "privilege."
                              << std::endl;
                    ;
                    ret = -1;
                }
            }
            CloseHandle(TokenHandle);
        }
        CloseHandle(currentProc);
    }

    return ret;
}

static int CreateImpersonatedProcess(HANDLE NewToken, LPCWSTR lpApplicationName,
                                     LPWSTR lpCommandLine) {

    STARTUPINFO lpStartupInfo = {0};
    PROCESS_INFORMATION lpProcessInformation = {0};

    lpStartupInfo.cb = sizeof(lpStartupInfo);

    TCHAR NPath[MAX_PATH];
    if (GetCurrentDirectory(MAX_PATH, NPath) == 0) {
        std::cout << "[!] Failed to get current directory" << std::endl;
        return -1;
    }

    if (!CreateProcessWithTokenW(
            NewToken, LOGON_WITH_PROFILE, lpApplicationName, lpCommandLine, 0,
            NULL, NPath, &lpStartupInfo, &lpProcessInformation)) {
        std::cout << "[!] Failed to create a new process with the stolen TOKEN"
                  << std::endl;
        return -1;
    }
    return 0;
}

static int StealToken(DWORD TargetPID, LPCWSTR lpApplicationName,
                      LPWSTR lpCommandLine) {
    HANDLE hProcess = NULL;
    HANDLE TokenHandle = NULL;
    HANDLE NewToken = NULL;
    int ret = -1;

    hProcess = OpenProcess(PROCESS_ALL_ACCESS, TRUE, TargetPID);

    if (!hProcess) {
        std::cout << "[!] Failed to obtain a HANDLE to the target PID"
                  << std::endl;
        return -1;
    }

    if (!OpenProcessToken(hProcess,
                          TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY | TOKEN_QUERY,
                          &TokenHandle)) {
        std::cout << "[!] Failed to obtain a HANDLE to the target TOKEN"
                  << std::endl;
        std::cout << GetLastError();
        goto _exit;
    }

    if (!ImpersonateLoggedOnUser(TokenHandle)) {
        std::cout << "[!] Failed to impersonate the TOKEN's user" << std::endl;
        goto _exit;
    }

    if (!DuplicateTokenEx(TokenHandle, TOKEN_ALL_ACCESS, NULL,
                          SecurityImpersonation, TokenPrimary, &NewToken)) {
        std::cout << "[!] Failed to duplicate the target TOKEN" << std::endl;
        goto _exit;
    }

    if (CreateImpersonatedProcess(NewToken, lpApplicationName, lpCommandLine) !=
        0) {
        std::cout << "[!] Failed to create impersonated process" << std::endl;
        goto _exit;
    }

    ret = 0;
_exit:
    if (NewToken)
        CloseHandle(NewToken);
    if (hProcess)
        CloseHandle(hProcess);
    if (TokenHandle)
        CloseHandle(TokenHandle);

    return ret;
}

extern "C" int RunAsSystem(LPCWSTR lpApplicationName, LPWSTR lpCommandLine) {
    int64_t winLogonPID = FindProcessPid(L"winlogon.exe", 1);
    if (winLogonPID < 0)
        return -1;

    if (EnableSeDebugPrivilegePrivilege() != 0)
        return -1;

    return StealToken((DWORD)winLogonPID, lpApplicationName, lpCommandLine);
}
