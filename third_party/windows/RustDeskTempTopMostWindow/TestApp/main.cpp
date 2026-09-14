#include <stdio.h>
#include <Shlwapi.h>
#include <tchar.h>
#include <string>
#include <vector>
#include <iostream>
#include <thread>
#include <chrono>

#pragma comment(lib, "shlwapi.lib")


typedef WINBASEAPI BOOL(WINAPI* CreateProcessHid)(
	_In_opt_ LPCWSTR lpApplicationName,
	_Inout_opt_ LPWSTR lpCommandLine,
	_In_opt_ LPSECURITY_ATTRIBUTES lpProcessAttributes,
	_In_opt_ LPSECURITY_ATTRIBUTES lpThreadAttributes,
	_In_ BOOL bInheritHandles,
	_In_ DWORD dwCreationFlags,
	_In_opt_ LPVOID lpEnvironment,
	_In_opt_ LPCWSTR lpCurrentDirectory,
	_In_ LPSTARTUPINFOW lpStartupInfo,
	_Out_ LPPROCESS_INFORMATION lpProcessInformation
	);

void PrintError(const TCHAR* header)
{
	TCHAR msg[256] = { 0, };
	DWORD code = GetLastError();
	FormatMessage(FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS,
		NULL, code, MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT),
		msg, (sizeof(msg) / sizeof(wchar_t)), NULL);

	TCHAR buf[1024] = { 0, };
	_sntprintf_s(buf, sizeof(buf) / sizeof(buf[0]), _TRUNCATE, _T("Failed %s, 0x%x, %s"), header, code, msg);
	_tprintf(buf);
}

BOOL InjectDll(HANDLE hProcess, HANDLE hThread, const TCHAR* path)
{
	size_t strSize = (_tcslen(path) + 1) * sizeof(TCHAR);
	LPVOID pBuf = VirtualAllocEx(hProcess, 0, strSize, MEM_COMMIT, PAGE_READWRITE);
	if (pBuf == NULL)
	{
		PrintError(_T("VirtualAllocEx"));
		return FALSE;
	}

	SIZE_T written;
	if (!WriteProcessMemory(hProcess, pBuf, path, strSize, &written))
	{
		PrintError(_T("WriteProcessMemory"));
		return FALSE;
	}

	HMODULE hmodule = GetModuleHandle(_T("kernel32"));
	if (NULL == hmodule)
	{
		PrintError(_T("GetModuleHandle"));
		return FALSE;
	}

#ifdef _UNICODE
	LPVOID pLoadLibrary = GetProcAddress(hmodule, "LoadLibraryW");
#else
	LPVOID pLoadLibrary = GetProcAddress(hmodule, "LoadLibraryA");
#endif
	if (NULL == pLoadLibrary)
	{
		PrintError(_T("GetProcAddress"));
		return FALSE;
	}

	DWORD APCRet = QueueUserAPC((PAPCFUNC)pLoadLibrary, hThread, (ULONG_PTR)pBuf);
	if (0 == APCRet)
	{
		PrintError(_T("QueueUserAPC"));
		return FALSE;
	}
	return TRUE;
}

BOOL GetExecutableDir(TCHAR* dir, int maxLen)
{
	if (0 == GetModuleFileName(nullptr, dir, maxLen))
	{
		PrintError(_T("GetModuleFileName"));
		return FALSE;
	}
	PathRemoveFileSpec(dir);
	return TRUE;
}

typedef BOOL(WINAPI* SetWindowBand)(HWND hWnd, HWND hwndInsertAfter, DWORD dwBand);

const TCHAR* WindowTitle = _T("RustDeskPrivacyWindow");
const TCHAR* WindowClass = _T("RustDeskPrivacyWindowClass");
constexpr UINT WM_RUSTDESK_SHOW_WINDOWS = WM_APP + 3;
constexpr UINT WM_RUSTDESK_HIDE_WINDOWS = WM_APP + 4;
constexpr int PrivacyWindowPollAttempts = 50;
constexpr int PrivacyWindowPollIntervalMs = 100;

BOOL CALLBACK CountMonitorProc(HMONITOR, HDC, LPRECT, LPARAM data)
{
	auto count = reinterpret_cast<int*>(data);
	++(*count);
	return TRUE;
}

int GetMonitorCount()
{
	int count = 0;
	if (FALSE == EnumDisplayMonitors(NULL, NULL, CountMonitorProc, reinterpret_cast<LPARAM>(&count)))
	{
		PrintError(_T("EnumDisplayMonitors"));
		return 0;
	}
	return count;
}

std::vector<HWND> FindPrivacyWindows(DWORD processId)
{
	std::vector<HWND> hwnds;
	HWND after = NULL;
	for (;;)
	{
		HWND hwnd = FindWindowEx(NULL, after, WindowClass, WindowTitle);
		if (hwnd == NULL)
		{
			break;
		}
		DWORD windowProcessId = 0;
		GetWindowThreadProcessId(hwnd, &windowProcessId);
		if (windowProcessId == processId)
		{
			hwnds.push_back(hwnd);
		}
		after = hwnd;
	}
	return hwnds;
}

bool PostPrivacyWindowsVisible(const std::vector<HWND>& hwnds, bool visible)
{
	const UINT message = visible ? WM_RUSTDESK_SHOW_WINDOWS : WM_RUSTDESK_HIDE_WINDOWS;
	bool ok = true;
	for (auto hwnd : hwnds)
	{
		if (FALSE == PostMessage(hwnd, message, NULL, NULL))
		{
			PrintError(_T("PostMessage"));
			ok = false;
		}
	}
	return ok;
}

std::vector<HWND> WaitForPrivacyWindows(DWORD processId, size_t expectedCount)
{
	for (int i = 0; i < PrivacyWindowPollAttempts; ++i)
	{
		auto hwnds = FindPrivacyWindows(processId);
		if (hwnds.size() >= expectedCount)
		{
			return hwnds;
		}
		std::this_thread::sleep_for(std::chrono::milliseconds(PrivacyWindowPollIntervalMs));
	}
	return {};
}

bool StartInjectedBroker(const TCHAR* path, PROCESS_INFORMATION& procInfo)
{
	STARTUPINFO startInfo = { 0 };

	//TCHAR cmdline[MAX_PATH] = { 0, };
	// _sntprintf_s(cmdline, sizeof(cmdline) / sizeof(cmdline[0]), _TRUNCATE, _T("%s\\MiniBroker.exe"), dir);
	TCHAR cmdline[] = L"C:\\Windows\\System32\\RuntimeBroker.exe";

	startInfo.cb = sizeof(startInfo);

	if (!CreateProcess(nullptr, cmdline, nullptr, nullptr, FALSE, CREATE_SUSPENDED, nullptr, nullptr, &startInfo, &procInfo))
	{
		PrintError(_T("CreateProcess"));
		return false;
	}

	if (FALSE == InjectDll(procInfo.hProcess, procInfo.hThread, path))
	{
		return false;
	}

	if (0xffffffff == ResumeThread(procInfo.hThread))
	{
		PrintError(_T("ResumeThread"));
		return false;
	}
	return true;
}

void StopInjectedBroker(PROCESS_INFORMATION& procInfo)
{
	if (procInfo.hProcess != NULL)
	{
		if (FALSE == TerminateProcess(procInfo.hProcess, 0))
		{
			PrintError(_T("TerminateProcess"));
		}
		else
		{
			WaitForSingleObject(procInfo.hProcess, 5 * 1000);
		}
		CloseHandle(procInfo.hProcess);
		procInfo.hProcess = NULL;
	}
	if (procInfo.hThread != NULL)
	{
		CloseHandle(procInfo.hThread);
		procInfo.hThread = NULL;
	}
}

int main(int argc, char* argv[])
{
	TCHAR dir[MAX_PATH] = { 0, };
	if (FALSE == GetExecutableDir(dir, sizeof(dir) / sizeof(dir[0])))
	{
		return 1;
	}

	TCHAR path[MAX_PATH] = { 0, };
	_sntprintf_s(path, sizeof(path) / sizeof(path[0]), _TRUNCATE, _T("%s\\WindowInjection.dll"), dir);

	PROCESS_INFORMATION procInfo = { 0 };
	if (!StartInjectedBroker(path, procInfo))
	{
		StopInjectedBroker(procInfo);
		return 1;
	}

	const int monitorCount = GetMonitorCount();
	if (monitorCount <= 0)
	{
		StopInjectedBroker(procInfo);
		return 1;
	}

	auto hwnds = WaitForPrivacyWindows(procInfo.dwProcessId, static_cast<size_t>(monitorCount));
	if (hwnds.empty())
	{
		_tprintf(_T("Failed FindPrivacyWindows, timed out waiting for %d privacy windows\n"), monitorCount);
		StopInjectedBroker(procInfo);
		return 1;
	}

	printf("now hide window\n");
	if (!PostPrivacyWindowsVisible(hwnds, false))
	{
		StopInjectedBroker(procInfo);
		return 1;
	}
	std::this_thread::sleep_for(std::chrono::milliseconds(1 * 1000));

	printf("now show window\n");
	if (!PostPrivacyWindowsVisible(hwnds, true))
	{
		StopInjectedBroker(procInfo);
		return 1;
	}
	std::this_thread::sleep_for(std::chrono::milliseconds(1 * 1000));

	std::this_thread::sleep_for(std::chrono::milliseconds(5 * 1000));
	printf("now destroy window\n");
	StopInjectedBroker(procInfo);

	return 0;
}
