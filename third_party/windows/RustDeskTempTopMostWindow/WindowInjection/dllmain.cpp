// Mainly from [MobileShell](https://github.com/ADeltaX/MobileShell)

#include "pch.h"

#include <algorithm>
#include <tchar.h>
#include <memory>
#include <type_traits>
#include <vector>

#include "./img.h"
#include "./bitmap_loader.h"

#pragma comment(lib, "gdi32.lib")


enum ZBID
{
	ZBID_DEFAULT = 0,
	ZBID_DESKTOP = 1,
	ZBID_UIACCESS = 2,
	ZBID_IMMERSIVE_IHM = 3,
	ZBID_IMMERSIVE_NOTIFICATION = 4,
	ZBID_IMMERSIVE_APPCHROME = 5,
	ZBID_IMMERSIVE_MOGO = 6,
	ZBID_IMMERSIVE_EDGY = 7,
	ZBID_IMMERSIVE_INACTIVEMOBODY = 8,
	ZBID_IMMERSIVE_INACTIVEDOCK = 9,
	ZBID_IMMERSIVE_ACTIVEMOBODY = 10,
	ZBID_IMMERSIVE_ACTIVEDOCK = 11,
	ZBID_IMMERSIVE_BACKGROUND = 12,
	ZBID_IMMERSIVE_SEARCH = 13,
	ZBID_GENUINE_WINDOWS = 14,
	ZBID_IMMERSIVE_RESTRICTED = 15,
	ZBID_SYSTEM_TOOLS = 16,
	ZBID_LOCK = 17,
	ZBID_ABOVELOCK_UX = 18,
};

#define  __imp_SetBrokeredForeground 2522

const TCHAR* WindowTitle = _T("RustDeskPrivacyWindow");
const TCHAR* ClassName = _T("RustDeskPrivacyWindowClass");
const TCHAR* DefaultBmpPath = _T("C:\\aa.bmp");
constexpr LONG PRIVACY_BITMAP_WIDTH = 1920;
constexpr LONG PRIVACY_BITMAP_HEIGHT = 1080;
constexpr UINT WM_RUSTDESK_REFRESH_WINDOWS = WM_APP + 1;
constexpr UINT WM_RUSTDESK_SHUTDOWN_WINDOWS = WM_APP + 2;
constexpr UINT WM_RUSTDESK_SHOW_WINDOWS = WM_APP + 3;
constexpr UINT WM_RUSTDESK_HIDE_WINDOWS = WM_APP + 4;

typedef enum tagDWMWINDOWATTRIBUTE {
	DWMWA_NCRENDERING_ENABLED = 1,
	DWMWA_NCRENDERING_POLICY,
	DWMWA_TRANSITIONS_FORCEDISABLED,
	DWMWA_ALLOW_NCPAINT,
	DWMWA_CAPTION_BUTTON_BOUNDS,
	DWMWA_NONCLIENT_RTL_LAYOUT,
	DWMWA_FORCE_ICONIC_REPRESENTATION,
	DWMWA_FLIP3D_POLICY,
	DWMWA_EXTENDED_FRAME_BOUNDS,
	DWMWA_HAS_ICONIC_BITMAP,
	DWMWA_DISALLOW_PEEK,
	DWMWA_EXCLUDED_FROM_PEEK,
	DWMWA_CLOAK,
	DWMWA_CLOAKED,
	DWMWA_FREEZE_REPRESENTATION,
	DWMWA_PASSIVE_UPDATE_MODE,
	DWMWA_USE_HOSTBACKDROPBRUSH,
	DWMWA_USE_IMMERSIVE_DARK_MODE = 20,
	DWMWA_WINDOW_CORNER_PREFERENCE = 33,
	DWMWA_BORDER_COLOR,
	DWMWA_CAPTION_COLOR,
	DWMWA_TEXT_COLOR,
	DWMWA_VISIBLE_FRAME_BORDER_THICKNESS,
	DWMWA_SYSTEMBACKDROP_TYPE,
	DWMWA_LAST,
} DWMWINDOWATTRIBUTE;

typedef HWND(WINAPI* CreateWindowInBand)(_In_ DWORD dwExStyle, _In_opt_ ATOM atom, _In_opt_ LPCWSTR lpWindowName, _In_ DWORD dwStyle, _In_ int X, _In_ int Y, _In_ int nWidth, _In_ int nHeight, _In_opt_ HWND hWndParent, _In_opt_ HMENU hMenu, _In_opt_ HINSTANCE hInstance, _In_opt_ LPVOID lpParam, DWORD band);
typedef BOOL(WINAPI* SetWindowBand)(HWND hWnd, HWND hwndInsertAfter, DWORD dwBand);
typedef BOOL(WINAPI* GetWindowBand)(HWND hWnd, PDWORD pdwBand);
typedef HDWP(WINAPI* DeferWindowPosAndBand)(_In_ HDWP hWinPosInfo, _In_ HWND hWnd, _In_opt_ HWND hWndInsertAfter, _In_ int x, _In_ int y, _In_ int cx, _In_ int cy, _In_ UINT uFlags, DWORD band, DWORD pls);
typedef HRESULT(WINAPI* DwmSetWindowAttributeFunc)(HWND hwnd, DWMWINDOWATTRIBUTE dwAttribute, LPCVOID pvAttribute, DWORD cbAttribute);

typedef BOOL(WINAPI* SetBrokeredForeground)(HWND hWnd);

// Window handles are owned by the privacy window thread. DllMain must not read
// g_hwnds; it signals the thread with PostThreadMessage instead.
std::vector<HWND> g_hwnds;
ATOM g_privacy_window_atom = 0;
HMODULE g_module = nullptr;
UINT g_zbid = ZBID_DEFAULT;
volatile LONG g_window_thread_id = 0;
// These window-state flags are read and written only on the privacy window thread.
bool g_refresh_pending = false;
bool g_refreshing_windows = false;
bool g_shutting_down_windows = false;
bool g_show_windows_after_refresh = false;
bool g_privacy_windows_visible = false;

// TODO: Read the register table to get the path.
// Or use hard code bitmap data.
TCHAR g_bmpPath[256] = { 0, };

bool g_loadFromMemory = true;

#ifdef WINDOWINJECTION_EXPORTS
BitmapLoader g_bitmapLoader(false);
#else
BitmapLoader g_bitmapLoader(true);
#endif

// Mainly from https://github.com/microsoft/Windows-classic-samples/blob/67a8cddc25880ebc64018e833f0bf51589fd4521/Samples/Win7Samples/winui/shell/appshellintegration/NotificationIcon/NotificationIcon.cpp#L360
VOID OnPaintGdi(HWND hwnd, HDC hdc);

// https://faithlife.codes/blog/2008/09/displaying_a_splash_screen_with_c_part_i/
// https://stackoverflow.com/a/66238748/1926020
VOID OnPaintGdiPlus(HWND hwnd, HDC hdc);

BOOL IsWindowsVersionOrGreater(
	DWORD os_major,
	DWORD os_minor,
	DWORD build_number,
	WORD service_pack_major,
	WORD service_pack_minor);
HRESULT SetDwmBoolWindowAttribute(HWND hwnd, DWMWINDOWATTRIBUTE attribute, BOOL enabled);
HWND CreateWin(HMODULE hModule, UINT zbid, const TCHAR* title, const TCHAR* classname, const RECT& monitorRect);
bool CreatePrivacyWindows(HMODULE hModule, UINT zbid, bool showWindows);
bool RefreshPrivacyWindows(HMODULE hModule, UINT zbid);
void SchedulePrivacyWindowRefresh();
bool HandlePrivacyThreadMessage(UINT message);
bool PostPrivacyThreadMessage(UINT message, bool reportError);
DWORD GetPrivacyWindowThreadId();
bool RemovePrivacyWindow(HWND hwnd);
bool DestroyPrivacyWindowsFrom(size_t begin);
HWND FailCreatedWindow(HWND hwnd, const TCHAR* caption);
void SetPrivacyWindowsVisible(bool visible);
void DebugLogLastError(const TCHAR* caption);
DWORD FinishPrivacyThread(DWORD result);

LRESULT CALLBACK TrashParentWndProc(HWND hwnd, UINT message, WPARAM wParam, LPARAM lParam)
{
	switch (message)
	{
	case WM_CREATE:
		break;

	case WM_DESTROY:
		if (!g_refreshing_windows && !g_shutting_down_windows)
		{
			if (FALSE != IsWindowVisible(hwnd))
			{
				g_show_windows_after_refresh = true;
			}
			if (RemovePrivacyWindow(hwnd))
			{
				SchedulePrivacyWindowRefresh();
			}
		}
		break;
	case WM_DISPLAYCHANGE:
	case WM_SETTINGCHANGE:
		SchedulePrivacyWindowRefresh();
		break;
	case WM_RUSTDESK_REFRESH_WINDOWS:
	case WM_RUSTDESK_SHOW_WINDOWS:
	case WM_RUSTDESK_HIDE_WINDOWS:
		(void)HandlePrivacyThreadMessage(message);
		break;

	case WM_WINDOWPOSCHANGING:
		return 0;
	case WM_CLOSE:
		HANDLE myself;
		myself = OpenProcess(PROCESS_ALL_ACCESS, false, GetCurrentProcessId());
		TerminateProcess(myself, 0);
		return true;

	case WM_PAINT:
		{
			// paint a pretty picture
			PAINTSTRUCT ps;
			HDC hdc = BeginPaint(hwnd, &ps);
			if (hdc)
			{
				// OnPaintGdi(hwnd, hdc);
				OnPaintGdiPlus(hwnd, hdc);
				EndPaint(hwnd, &ps);
			}
		}
		break;

	default:
		break;
	}

	return DefWindowProc(hwnd, message, wParam, lParam);
}

void ShowErrorMsg(const TCHAR* caption)
{
	DWORD code = GetLastError();
	TCHAR msg[256] = { 0, };
	FormatMessage(FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS,
		NULL, code, MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT),
		msg, (sizeof(msg) / sizeof(msg[0])), NULL);

#ifdef WINDOWINJECTION_EXPORTS
	TCHAR buf[1024] = { 0, };
	_sntprintf_s(buf, sizeof(buf) / sizeof(buf[0]), _TRUNCATE, _T("%s: %s, code 0x%x\n"), caption, msg, code);
	OutputDebugString(buf);
#else
	_tprintf(_T("%s: %s, code 0x%x\n"), caption, msg, code);
#endif
}

void ShowBitmapLoaderErrorMsg(const TCHAR* msg, EBitmapLoader code, const TCHAR* detail)
{
#ifdef WINDOWINJECTION_EXPORTS
	TCHAR buf[1024] = { 0, };
	_sntprintf_s(
		buf,
		sizeof(buf) / sizeof(buf[0]),
		_TRUNCATE,
		_T("%s, %s, code %d"),
		msg,
		detail,
		static_cast<int>(code));
	OutputDebugString(buf);
#else
	_tprintf(_T("BitmapLoader: %s, %s, code %d\n"), msg, detail, static_cast<int>(code));
#endif
}

HWND CreateWin(HMODULE hModule, UINT zbid, const TCHAR* title, const TCHAR* classname, const RECT& monitorRect)
{
	HINSTANCE hInstance = hModule;
	WNDCLASSEX wndParentClass;

	wndParentClass.cbSize = sizeof(WNDCLASSEX);
	wndParentClass.cbClsExtra = 0;
	wndParentClass.cbWndExtra = 0;
	wndParentClass.hIcon = NULL;
	wndParentClass.lpszMenuName = NULL;
	wndParentClass.hIconSm = NULL;
	wndParentClass.lpfnWndProc = TrashParentWndProc;
	wndParentClass.hInstance = hInstance;
	wndParentClass.style = CS_HREDRAW | CS_VREDRAW;
	wndParentClass.hCursor = LoadCursor(0, IDC_ARROW);
	wndParentClass.hbrBackground = (HBRUSH)GetStockObject(BLACK_BRUSH);
	wndParentClass.lpszClassName = classname;

	auto class_atom = g_privacy_window_atom;
	if (class_atom == 0)
	{
		class_atom = RegisterClassEx(&wndParentClass);
		if (class_atom == 0)
		{
			ShowErrorMsg(_T("RegisterClassEx"));
			return nullptr;
		}
		g_privacy_window_atom = class_atom;
	}

	const auto hpath = GetModuleHandle(_T("user32.dll"));
	if (hpath == 0)
	{
		ShowErrorMsg(_T("GetModuleHandle user32.dll"));
		return nullptr;
	}

	const auto pCreateWindowInBand = CreateWindowInBand(GetProcAddress(hpath, "CreateWindowInBand"));
	if (!pCreateWindowInBand)
	{
		ShowErrorMsg(_T("GetProcAddress CreateWindowInBand"));
		return nullptr;
	}

	HWND hwnd = pCreateWindowInBand(
		WS_EX_TOPMOST | WS_EX_NOACTIVATE,
		class_atom,
		NULL,
		0x80000000,
		0, 0, 0, 0,
		NULL,
		NULL,
		wndParentClass.hInstance,
		LPVOID(class_atom),
		zbid);
	if (!hwnd)
	{
		ShowErrorMsg(_T("CreateWindowInBand"));
		return nullptr;
	}

	if (FALSE == SetWindowText(hwnd, title))
	{
		return FailCreatedWindow(hwnd, _T("SetWindowText"));
	}

	//HRGN hrg = CreateRoundRectRgn(
	//	mi.rcMonitor.left,
	//	mi.rcMonitor.top,
	//	mi.rcMonitor.right - mi.rcMonitor.left,
	//	mi.rcMonitor.bottom - mi.rcMonitor.top,
	//	8,
	//	8);
	//if (NULL == hrg)
	//{
	//	ShowErrorMsg(_T("CreateRoundRectRgn"));
	//	return nullptr;
	//}

	//if (0 == SetWindowRgn(hwnd, hrg, true))
	//{
	//	ShowErrorMsg(_T("SetWindowRgn"));
	//	return nullptr;
	//}

	//const auto pSetBrokeredForeground = SetBrokeredForeground(GetProcAddress(hpath, MAKEINTRESOURCEA(__imp_SetBrokeredForeground)));
	//pSetBrokeredForeground(hwnd); //Works only if the window is created in ZBID_GENUINE_WINDOWS band.
	//
	//const auto pSetWindowBand = SetWindowBand(GetProcAddress(hpath, "SetWindowBand"));
	//pSetWindowBand(hwnd, HWND_TOPMOST, ZBID_ABOVELOCK_UX); //This still doesn't in any case.

	if (0 == SetWindowPos(
		hwnd,
		nullptr,
		monitorRect.left,
		monitorRect.top,
		monitorRect.right - monitorRect.left,
		monitorRect.bottom - monitorRect.top,
		SWP_NOZORDER | SWP_NOACTIVATE))
	{
		return FailCreatedWindow(hwnd, _T("SetWindowPos"));
	}

	auto setLongRes = SetWindowLong(
		hwnd,
		GWL_EXSTYLE,
		GetWindowLong(hwnd, GWL_EXSTYLE) | WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE);
	if (0 == setLongRes)
	{
		return FailCreatedWindow(hwnd, _T("SetWindowLong"));
	}

	// Keep the layered window fully opaque while enabling transparent click-through.
	if (FALSE == SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA))
	{
		return FailCreatedWindow(hwnd, _T("SetLayeredWindowAttributes"));
	}

	// Keep the privacy overlay visible when taskbar Peek previews another window.
	// Do not use DWMWA_CLOAK here because it hides the privacy window itself.
	if (FAILED(SetDwmBoolWindowAttribute(hwnd, DWMWA_EXCLUDED_FROM_PEEK, TRUE)))
	{
		return FailCreatedWindow(hwnd, _T("SetDwmBoolWindowAttribute DWMWA_EXCLUDED_FROM_PEEK"));
	}

	if (IsWindowsVersionOrGreater(10, 0, 19041, 0, 0) == TRUE)
	{
		(void)SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE);
	}

	if (FALSE == UpdateWindow(hwnd))
	{
		return FailCreatedWindow(hwnd, _T("UpdateWindow"));
	}

	return hwnd;
}

HRESULT SetDwmBoolWindowAttribute(HWND hwnd, DWMWINDOWATTRIBUTE attribute, BOOL enabled) {
	HRESULT result;
	bool loadedDwmapi = false;
	HMODULE hMod = GetModuleHandle(TEXT("dwmapi.dll"));
	if (!hMod) {
		hMod = LoadLibrary(TEXT("dwmapi.dll"));
		loadedDwmapi = hMod != nullptr;
	}
	if (hMod) {
		DwmSetWindowAttributeFunc pDwmSetWindowAttribute = (DwmSetWindowAttributeFunc)GetProcAddress(hMod, "DwmSetWindowAttribute");
		if (pDwmSetWindowAttribute) {
			result = pDwmSetWindowAttribute(hwnd, attribute, &enabled, sizeof(enabled));
			if (FAILED(result)) {
				SetLastError(HRESULT_CODE(result));
			}
		}
		else {
			result = HRESULT_FROM_WIN32(GetLastError());
		}
		if (loadedDwmapi) {
			FreeLibrary(hMod);
		}
	}
	else {
		result = HRESULT_FROM_WIN32(GetLastError());
	}
	return result;
}

void DebugLogLastError(const TCHAR* caption)
{
	DWORD code = GetLastError();
	TCHAR msg[512] = { 0, };
	_sntprintf_s(msg, sizeof(msg) / sizeof(msg[0]), _TRUNCATE, _T("%s, code 0x%x\n"), caption, code);
	OutputDebugString(msg);
}

BOOL CALLBACK EnumMonitorRectProc(HMONITOR hmon, HDC, LPRECT, LPARAM lParam)
{
	auto rects = reinterpret_cast<std::vector<RECT> *>(lParam);
	MONITORINFO mi = { sizeof(mi) };
	if (0 == GetMonitorInfo(hmon, &mi))
	{
		DebugLogLastError(_T("GetMonitorInfo"));
		return FALSE;
	}
	rects->push_back(mi.rcMonitor);
	return TRUE;
}

bool GetMonitorRects(std::vector<RECT>& rects)
{
	rects.clear();
	return FALSE != EnumDisplayMonitors(nullptr, nullptr, EnumMonitorRectProc, reinterpret_cast<LPARAM>(&rects));
}

DWORD GetPrivacyWindowThreadId()
{
	return static_cast<DWORD>(InterlockedCompareExchange(&g_window_thread_id, 0, 0));
}

bool PostPrivacyThreadMessage(UINT message, bool reportError)
{
	const auto threadId = GetPrivacyWindowThreadId();
	if (threadId == 0)
	{
		return false;
	}
	if (FALSE == PostThreadMessage(threadId, message, NULL, NULL))
	{
		if (reportError)
		{
			ShowErrorMsg(_T("PostThreadMessage"));
		}
		return false;
	}
	return true;
}

void SchedulePrivacyWindowRefresh()
{
	if (g_refresh_pending)
	{
		return;
	}
	g_refresh_pending = true;
	if (!PostPrivacyThreadMessage(WM_RUSTDESK_REFRESH_WINDOWS, true))
	{
		g_refresh_pending = false;
	}
}

bool RemovePrivacyWindow(HWND hwnd)
{
	const auto it = std::find(g_hwnds.begin(), g_hwnds.end(), hwnd);
	if (it == g_hwnds.end())
	{
		return false;
	}
	g_hwnds.erase(it);
	return true;
}

bool IsAnyPrivacyWindowVisible()
{
	for (auto hwnd : g_hwnds)
	{
		if (FALSE != IsWindowVisible(hwnd))
		{
			return true;
		}
	}
	return false;
}

void SetPrivacyWindowsVisible(bool visible)
{
	g_privacy_windows_visible = visible;
	if (!visible)
	{
		g_show_windows_after_refresh = false;
	}
	for (auto hwnd : g_hwnds)
	{
		ShowWindow(hwnd, visible ? SW_SHOW : SW_HIDE);
	}
}

bool ResizePrivacyWindow(HWND hwnd, const RECT& monitorRect)
{
	if (0 == SetWindowPos(
		hwnd,
		nullptr,
		monitorRect.left,
		monitorRect.top,
		monitorRect.right - monitorRect.left,
		monitorRect.bottom - monitorRect.top,
		SWP_NOZORDER | SWP_NOACTIVATE))
	{
		ShowErrorMsg(_T("SetWindowPos"));
		return false;
	}
	InvalidateRect(hwnd, nullptr, TRUE);
	return true;
}

bool DestroyPrivacyWindowsFrom(size_t begin)
{
	g_refreshing_windows = true;
	bool ok = true;
	std::vector<HWND> remaining(g_hwnds.begin(), g_hwnds.begin() + begin);
	for (size_t i = begin; i < g_hwnds.size(); ++i)
	{
		const auto hwnd = g_hwnds[i];
		if (FALSE == DestroyWindow(hwnd))
		{
			ShowErrorMsg(_T("DestroyWindow"));
			ok = false;
			if (FALSE != IsWindow(hwnd))
			{
				remaining.push_back(hwnd);
			}
		}
	}
	g_refreshing_windows = false;
	g_hwnds.swap(remaining);
	return ok;
}

HWND FailCreatedWindow(HWND hwnd, const TCHAR* caption)
{
	ShowErrorMsg(caption);
	if (hwnd)
	{
		const auto wasRefreshing = g_refreshing_windows;
		g_refreshing_windows = true;
		DestroyWindow(hwnd);
		g_refreshing_windows = wasRefreshing;
	}
	return nullptr;
}

bool CreatePrivacyWindows(HMODULE hModule, UINT zbid, bool showWindows)
{
	std::vector<RECT> monitorRects;
	if (!GetMonitorRects(monitorRects))
	{
		ShowErrorMsg(_T("EnumDisplayMonitors"));
		return false;
	}
	if (monitorRects.empty())
	{
		return false;
	}

	const auto createdBegin = g_hwnds.size();
	for (const auto& monitorRect : monitorRects)
	{
		auto hwnd = CreateWin(hModule, zbid, WindowTitle, ClassName, monitorRect);
		if (!hwnd)
		{
			(void)DestroyPrivacyWindowsFrom(createdBegin);
			return false;
		}
		if (showWindows)
		{
			ShowWindow(hwnd, SW_SHOW);
		}
		g_hwnds.push_back(hwnd);
	}
	return true;
}

bool RefreshPrivacyWindows(HMODULE hModule, UINT zbid)
{
	if (g_shutting_down_windows)
	{
		return true;
	}

	std::vector<RECT> monitorRects;
	if (!GetMonitorRects(monitorRects))
	{
		ShowErrorMsg(_T("EnumDisplayMonitors"));
		return false;
	}
	if (monitorRects.empty())
	{
		return false;
	}

	const bool showWindows = g_show_windows_after_refresh || g_privacy_windows_visible || IsAnyPrivacyWindowVisible();
	const auto createdBegin = g_hwnds.size();
	for (size_t i = 0; i < monitorRects.size(); ++i)
	{
		if (i < g_hwnds.size())
		{
			if (!ResizePrivacyWindow(g_hwnds[i], monitorRects[i]))
			{
				return false;
			}
			continue;
		}

		auto hwnd = CreateWin(hModule, zbid, WindowTitle, ClassName, monitorRects[i]);
		if (!hwnd)
		{
			(void)DestroyPrivacyWindowsFrom(createdBegin);
			return false;
		}
		if (showWindows)
		{
			ShowWindow(hwnd, SW_SHOW);
		}
		g_hwnds.push_back(hwnd);
	}

	if (g_hwnds.size() > monitorRects.size())
	{
		const bool ok = DestroyPrivacyWindowsFrom(monitorRects.size());
		if (ok)
		{
			g_show_windows_after_refresh = false;
		}
		return ok;
	}
	g_show_windows_after_refresh = false;
	return true;
}

bool HandlePrivacyThreadMessage(UINT message)
{
	switch (message)
	{
	case WM_RUSTDESK_REFRESH_WINDOWS:
		g_refresh_pending = false;
		(void)RefreshPrivacyWindows(g_module, g_zbid);
		return true;
	case WM_RUSTDESK_SHOW_WINDOWS:
		SetPrivacyWindowsVisible(true);
		return true;
	case WM_RUSTDESK_HIDE_WINDOWS:
		SetPrivacyWindowsVisible(false);
		return true;
	case WM_RUSTDESK_SHUTDOWN_WINDOWS:
		g_shutting_down_windows = true;
		(void)DestroyPrivacyWindowsFrom(0);
		PostQuitMessage(0);
		return true;
	default:
		return false;
	}
}

DWORD FinishPrivacyThread(DWORD result)
{
	InterlockedExchange(&g_window_thread_id, 0);
	return result;
}

DWORD WINAPI UwU(LPVOID lpParam)
{
	g_module = reinterpret_cast<HMODULE>(lpParam);

	MSG msg;
	PeekMessage(&msg, nullptr, WM_USER, WM_USER, PM_NOREMOVE);
	InterlockedExchange(&g_window_thread_id, GetCurrentThreadId());

#ifdef WINDOWINJECTION_EXPORTS
	auto initRes = g_bitmapLoader.Initialize(true);
#else
	auto initRes = g_bitmapLoader.Initialize(true);
#endif
	if (EBitmapLoader::kOk != initRes)
	{
		ShowBitmapLoaderErrorMsg(_T("Initialize"), initRes, g_bitmapLoader.GetLastErrMsg());
		return FinishPrivacyThread(0);
	}

	long rect[4] = { 0, 0, PRIVACY_BITMAP_WIDTH, PRIVACY_BITMAP_HEIGHT };
	auto DIBres = EBitmapLoader::kErrUnknown;
	if (g_loadFromMemory)
	{
		DIBres = g_bitmapLoader.CreateDIBFromMemory(
			(char*)(g_img),
			static_cast<unsigned int>(g_imgLen),
			rect);
	}
	else
	{
		DIBres = g_bitmapLoader.CreateDIBFromFile(DefaultBmpPath, rect);
	}
	if (EBitmapLoader::kOk != DIBres)
	{
		ShowBitmapLoaderErrorMsg(_T("CreateDIBFromFile"), DIBres, g_bitmapLoader.GetLastErrMsg());
		return FinishPrivacyThread(0);
	}

#ifdef WINDOWINJECTION_EXPORTS
	g_zbid = ZBID_ABOVELOCK_UX;
#else
	g_zbid = ZBID_DESKTOP;
#endif

	if (!CreatePrivacyWindows(g_module, g_zbid, false))
	{
		return FinishPrivacyThread(0);
	}

#ifndef WINDOWINJECTION_EXPORTS
	for (auto hwnd : g_hwnds)
	{
		ShowWindow(hwnd, SW_SHOW);
	}
#endif

	while (GetMessage(&msg, nullptr, 0, 0))
	{
		if (HandlePrivacyThreadMessage(msg.message))
		{
			continue;
		}
		TranslateMessage(&msg);
		DispatchMessage(&msg);
	}

	return FinishPrivacyThread(0);
}

void OnPaintGdi(HWND hwnd, HDC hdc)
{
	if (!hdc)
	{
		return;
	}

	static HBITMAP hbmp = NULL;
	if (hbmp == NULL)
	{
		// hbmp = (HBITMAP)LoadImage(hInstance, MAKEINTRESOURCE(103), IMAGE_BITMAP, 0, 0, 0);

		// Resouce cannot be loaded in a DLL with different location.
		// https://stackoverflow.com/a/2197447/1926020
		const TCHAR* bmpPath = _tcslen(g_bmpPath) > 0 ? g_bmpPath: DefaultBmpPath;
		hbmp = (HBITMAP)LoadImageW(NULL, bmpPath, IMAGE_BITMAP, 0, 0, LR_DEFAULTSIZE | LR_LOADFROMFILE);

		// DeleteObject(hbmp)
	}
	if (hbmp == NULL)
	{
		// ShowErrorMsg(_T("LoadImage"));
		return;
	}

	RECT rcClient;
	if (FALSE == GetClientRect(hwnd, &rcClient))
	{
		// ShowErrorMsg(_T("GetClientRect"));
		return;
	}

	HDC hdcMem = CreateCompatibleDC(hdc);
	if (!hdcMem)
	{
		// ShowErrorMsg(_T("CreateCompatibleDC"));
		return;
	}

	HGDIOBJ hBmpOld = SelectObject(hdcMem, hbmp);
	if (FALSE == BitBlt(hdc, 0, 0, rcClient.right, rcClient.bottom, hdcMem, 0, 0, SRCCOPY))
	{
		// ShowErrorMsg(_T("SelectObject"));
		DeleteDC(hdcMem);
		return;
	}
	SelectObject(hdcMem, hBmpOld);
	DeleteDC(hdcMem);
}

VOID OnPaintGdiPlus(HWND hwnd, HDC hdc)
{
	if (!hdc)
	{
		return;
	}

	auto bitmap = g_bitmapLoader.GetBitmap();
	if (bitmap)
	{
		RECT rcClient;
		if (FALSE == GetClientRect(hwnd, &rcClient))
		{
			return;
		}

		Gdiplus::Graphics graphics(hdc);
		graphics.Clear(Gdiplus::Color::Black);

		const auto client_w = static_cast<Gdiplus::REAL>(rcClient.right - rcClient.left);
		const auto client_h = static_cast<Gdiplus::REAL>(rcClient.bottom - rcClient.top);
		const auto bmp_w = static_cast<Gdiplus::REAL>(bitmap->GetWidth());
		const auto bmp_h = static_cast<Gdiplus::REAL>(bitmap->GetHeight());
		const auto scale_w = client_w / bmp_w;
		const auto scale_h = client_h / bmp_h;
		const auto scale = scale_w < scale_h ? scale_w : scale_h;
		const auto dest_w = bmp_w * scale;
		const auto dest_h = bmp_h * scale;
		const auto dest_x = (client_w - dest_w) / 2.0f;
		const auto dest_y = (client_h - dest_h) / 2.0f;
		const Gdiplus::RectF dest = Gdiplus::RectF(dest_x, dest_y, dest_w, dest_h);

		graphics.DrawImage(bitmap, dest);
	}
}

#ifdef WINDOWINJECTION_EXPORTS

// https://docs.microsoft.com/en-us/windows/win32/dlls/dllmain
BOOL APIENTRY DllMain(HMODULE hModule, DWORD  ulReasonForCall, LPVOID lpReserved)
{
	// https://tbhaxor.com/loading-dlls-using-cpp-in-windows/
	switch (ulReasonForCall)
	{
	case DLL_PROCESS_ATTACH:
		// Initialize once for each new process.
		{
			if (FALSE == DisableThreadLibraryCalls(hModule))
			{
				DebugLogLastError(_T("DisableThreadLibraryCalls"));
			}

			HANDLE thread = CreateThread(nullptr, 0, UwU, hModule, NULL, NULL);
			if (thread)
			{
				CloseHandle(thread);
			}
			else
			{
				DebugLogLastError(_T("CreateThread"));
			}
		}
		break;
	case DLL_THREAD_ATTACH:
		// Do thread-specific initialization.
		break;
	case DLL_THREAD_DETACH:
		// Do thread-specific cleanup.
		break;
	case DLL_PROCESS_DETACH:
		// Perform any necessary cleanup.
		if (lpReserved == nullptr)
		{
			// Dynamic FreeLibrary unload is unsupported and best-effort only.
			// RustDesk tears this broker down by terminating the host process,
			// and waiting for the privacy thread here would run under the loader lock.
			(void)PostPrivacyThreadMessage(WM_RUSTDESK_SHUTDOWN_WINDOWS, false);
		}
		break;
	default:
		break;
	}

	return TRUE;
}

#else

int main(int argc, char* argv[])
{
	HMODULE hInstance = GetModuleHandle(nullptr);
	if (!hInstance)
	{
		printf("Failed to GetModuleHandle, 0x%x\n", GetLastError());
		return 0;
	}

	return UwU(hInstance);
}

#endif

// https://github.com/nodejs/node-convergence-archive/blob/e11fe0c2777561827cdb7207d46b0917ef3c42a7/deps/uv/src/win/util.c#L780
BOOL IsWindowsVersionOrGreater(
	DWORD os_major,
	DWORD os_minor,
	DWORD build_number,
	WORD service_pack_major,
	WORD service_pack_minor)
{
	OSVERSIONINFOEX osvi;
	DWORDLONG condition_mask = 0;
	int op = VER_GREATER_EQUAL;

	/* Initialize the OSVERSIONINFOEX structure. */
	ZeroMemory(&osvi, sizeof(OSVERSIONINFOEX));
	osvi.dwOSVersionInfoSize = sizeof(OSVERSIONINFOEX);
	osvi.dwMajorVersion = os_major;
	osvi.dwMinorVersion = os_minor;
	osvi.dwBuildNumber = build_number;
	osvi.wServicePackMajor = service_pack_major;
	osvi.wServicePackMinor = service_pack_minor;

	/* Initialize the condition mask. */
	VER_SET_CONDITION(condition_mask, VER_MAJORVERSION, op);
	VER_SET_CONDITION(condition_mask, VER_MINORVERSION, op);
	VER_SET_CONDITION(condition_mask, VER_SERVICEPACKMAJOR, op);
	VER_SET_CONDITION(condition_mask, VER_SERVICEPACKMINOR, op);

	/* Perform the test. */
	return VerifyVersionInfo(
		&osvi,
		VER_MAJORVERSION | VER_MINORVERSION |
		VER_SERVICEPACKMAJOR | VER_SERVICEPACKMINOR,
		condition_mask);
}
