#pragma once

#include <tchar.h>
#include <windows.h>
#include <wincodec.h>
#include <commdlg.h>
#include <gdiplus.h>

#include <string>
#include <memory>
#include <functional>

enum class EBitmapLoader
{
    kOk = 0,
    kErrCoInit = 1001,
    kErrGdiplusStartup = 1002,
    kErrCreateFactor = 1003,
    kErrCreateDecoder = 1101,
    kErrGetFrame = 1102,
    kErrQueryFrameInterface = 1103,
    kErrConvertBitmapSource = 1104,
    kErrCreateDIBSection = 1105,
    kErrCreateStream = 1106,
    kErrStreamInitFromMem = 1107,

    kErrCreateBitmapScaler = 1201,
    kErrScalerInit = 1202,
    kErrCreateFormatConverter = 1203,
    kErrConverterInit = 1204,
    kErrConverterQueryInterface = 1205,

    kErrGetPixelFormat = 1301,
    kErrPixelFormatMismatch = 1302,
    kErrGetPixelSize = 1303,
    kErrSizeMult = 1304,
    kErrCopyPixels = 1305,
    kErrBitmapStatus = 1306,

    kErrAllocMemory = 9901,
    kErrUnknown = 9999,
};


// Mainly from
// https://github.com/microsoft/Windows-classic-samples/blob/main/Samples/Win7Samples/multimedia/wic/wicviewergdiplus
// https://github.com/microsoft/Windows-classic-samples/blob/main/Samples/Win7Samples/multimedia/Direct2D/DXGISample/DxgiSample.cpp

class BitmapLoader
{
public:
    BitmapLoader(bool printMsg = true);
    ~BitmapLoader();

public:
    // https://docs.microsoft.com/en-us/windows/win32/api/combaseapi/nf-combaseapi-coinitializeex
    // 
    // Because there is no way to control the order in which in-process servers are loaded or unloaded,
    // do not call CoInitialize, CoInitializeEx, or CoUninitialize from the DllMain function.
    EBitmapLoader Initialize(bool coinit);
    void Uninitialize();

    // https://github.com/microsoft/Windows-classic-samples/blob/main/Samples/Win7Samples/multimedia/wic/wicviewergdiplus
    // rect[0] - left, rect[1] - top, rect[2] - right, rect[3] - bottom
    EBitmapLoader CreateDIBFromFile(const std::wstring& filename, const long rect[4]);

    // https://github.com/microsoft/Windows-classic-samples/blob/main/Samples/Win7Samples/multimedia/Direct2D/DXGISample/DxgiSample.cpp
    // rect[0] - left, rect[1] - top, rect[2] - right, rect[3] - bottom
    EBitmapLoader CreateDIBFromMemory(char* buf, unsigned int  len, const long rect[4]);

    inline Gdiplus::Bitmap* GetBitmap() const
    {
        return m_pGdiPlusBitmap;
    }

    const TCHAR* GetLastErrMsg() const
    {
        return m_lastErrMsg;
    }

private:
    EBitmapLoader CreateDIBFromDecoder(IWICBitmapDecoder* pDecoder, const long rect[4]);
    EBitmapLoader ConvertBitmapSource(const long rect[4], IWICBitmapSource** ppToRenderBitmapSource);
    EBitmapLoader CreateDIBSectionFromBitmapSource(IWICBitmapSource* pToRenderBitmapSource);

    void DeleteBufferAndBmp();

    void SetLastMsg(const TCHAR* format, ...);

private:
    template<class T>
    struct SimpleRaii
    {
        using Deletor = std::function<void(T t)>;

        SimpleRaii(T t1, Deletor d)
            : t(t1), del(d)
        {
        }

        void Set(T t1)
        {
            if (del)
            {
                del(t);
            }
            t = t1;
        }

        ~SimpleRaii()
        {
            if (del)
            {
                del(t);
            }
        }

        T t;
        Deletor del;
    };

    using CoInitHandler = SimpleRaii<bool>;
    using GdiPlusInitHandler = SimpleRaii<ULONG_PTR>;

private:
    bool                m_printMsg;

    HINSTANCE           m_hInst;
    Gdiplus::Bitmap*    m_pGdiPlusBitmap;
    BYTE*               m_pbBuffer;

    IWICImagingFactory* m_pIWICFactory;
    IWICBitmapSource*   m_pOriginalBitmapSource;

    CoInitHandler       m_hCoInit;
    GdiPlusInitHandler  m_hGdiPlusInit;

    const TCHAR* const m_msgHeader = _T("BitmapLoader: ");
    TCHAR             m_lastErrMsg[1024] = { 0, };
};
