#include "pch.h"

#ifndef WINVER                  // Allow use of features specific to Windows XP or later.
#define WINVER 0x0501           // Change this to the appropriate value to target other versions of Windows.
#endif

#ifndef _WIN32_WINNT            // Allow use of features specific to Windows XP or later.
#define _WIN32_WINNT 0x0501     // Change this to the appropriate value to target other versions of Windows.
#endif

#ifndef _WIN32_WINDOWS          // Allow use of features specific to Windows 98 or later.
#define _WIN32_WINDOWS 0x0410   // Change this to the appropriate value to target Windows Me or later.
#endif

#ifndef _WIN32_IE               // Allow use of features specific to IE 6.0 or later.
#define _WIN32_IE 0x0600        // Change this to the appropriate value to target other versions of IE.
#endif

#define WIN32_LEAN_AND_MEAN     // Exclude rarely-used stuff from Windows headers

#include <stdio.h>
#include <strsafe.h>

#include "./bitmap_loader.h"

#pragma comment(lib, "Gdiplus.lib")
#pragma comment(lib, "windowscodecs.lib")


namespace
{
    template <typename T>
    inline void SafeRelease(T*& p)
    {
        if (NULL != p)
        {
            p->Release();
            p = NULL;
        }
    }

    template <class T>
    struct ReleaseHolder
    {
        ReleaseHolder(T& t): t(t) {}
        ~ReleaseHolder() { SafeRelease(t); }
        T& t;
    };

#define RELEASE_AUTO(p) ReleaseHolder<decltype(p)> p##_ReleaseHolder(p);
#define CHECK_RETURN(f) { auto r = (f); if (r != EBitmapLoader::kOk) { return r; } }
}


BitmapLoader::BitmapLoader(bool printMsg)
    : m_printMsg(printMsg)
    , m_hInst(nullptr)
    , m_pGdiPlusBitmap(nullptr)
    , m_pbBuffer(nullptr)
    , m_pIWICFactory(nullptr)
    , m_pOriginalBitmapSource(nullptr)
    , m_hCoInit(false, [](bool uninit) { if (uninit) CoUninitialize(); })
    , m_hGdiPlusInit(0, [](ULONG_PTR token) { if (token != 0) Gdiplus::GdiplusShutdown(token); })
{
}

BitmapLoader::~BitmapLoader()
{
    Uninitialize();
}

enum EBitmapLoader BitmapLoader::Initialize(bool coinit)
{
    HRESULT hr = S_OK;
    SetLastMsg(_T("Sucess"));

    if (coinit)
    {
        hr = CoInitializeEx(NULL, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE);
        if (FAILED(hr))
        {
            SetLastMsg(_T("CoInitializeEx failed\n"));
            return EBitmapLoader::kErrCoInit;
        }

        m_hCoInit.Set(true);
    }

    // Initialize GDI+.
    Gdiplus::GdiplusStartupInput gdiplusStartupInput;
    ULONG_PTR gdiplusToken;

    // Checking return status from GdiplusStartup 
    Gdiplus::Status status = Gdiplus::GdiplusStartup(&gdiplusToken, &gdiplusStartupInput, NULL);
    if (status != Gdiplus::Ok)
    {
        SetLastMsg(_T("Failed to GdiplusStartup, status %d\n"), static_cast<int>(status));
        if (m_printMsg)
        {
            _tprintf(m_lastErrMsg);
        }
        return EBitmapLoader::kErrGdiplusStartup;
    }
    m_hGdiPlusInit.Set(gdiplusToken);

    // Create WIC factory
    hr = CoCreateInstance(
        CLSID_WICImagingFactory,
        NULL,
        CLSCTX_INPROC_SERVER,
        IID_PPV_ARGS(&m_pIWICFactory)
    );
    if (FAILED(hr))
    {
        SetLastMsg(_T("Failed to CoCreateInstance, result 0x%lx\n"), hr);
        if (m_printMsg)
        {
            _tprintf(m_lastErrMsg);
        }
        return EBitmapLoader::kErrCreateFactor;
    }
    
    return EBitmapLoader::kOk;
}

void BitmapLoader::Uninitialize()
{
    SafeRelease(m_pIWICFactory);
    SafeRelease(m_pOriginalBitmapSource);
    DeleteBufferAndBmp();
}

EBitmapLoader BitmapLoader::CreateDIBFromFile(const std::wstring& filename, const long rect[4])
{
    HRESULT hr = S_OK;

    if (!m_pIWICFactory)
    {
        SetLastMsg(_T("empty IWICImagingFactory pointer\n"));
        if (m_printMsg)
        {
            _tprintf(m_lastErrMsg);
        }
        return EBitmapLoader::kErrCreateFactor;
    }

    // Step 2: Decode the source image to IWICBitmapSource

    // Create a decoder
    IWICBitmapDecoder* pDecoder = NULL;
    RELEASE_AUTO(pDecoder);
    hr = m_pIWICFactory->CreateDecoderFromFilename(
        filename.c_str(),                // Image to be decoded
        NULL,                            // Do not prefer a particular vendor
        GENERIC_READ,                    // Desired read access to the file
        WICDecodeMetadataCacheOnDemand,  // Cache metadata when needed
        &pDecoder                        // Pointer to the decoder
    );
    if (FAILED(hr))
    {
        SetLastMsg(_T("Failed CreateDecoderFromFilename, 0x%lx\n"), hr);
        if (m_printMsg)
        {
            _tprintf(m_lastErrMsg);
        }
        return EBitmapLoader::kErrCreateDecoder;
    }

    CHECK_RETURN(CreateDIBFromDecoder(pDecoder, rect));

    return EBitmapLoader::kOk;
}

EBitmapLoader BitmapLoader::CreateDIBFromMemory(char* buf, unsigned int  len, const long rect[4])
{
    HRESULT hr = S_OK;

    if (!m_pIWICFactory)
    {
        SetLastMsg(_T("empty IWICImagingFactory pointer\n"));
        if (m_printMsg)
        {
            _tprintf(m_lastErrMsg);
        }
        return EBitmapLoader::kErrCreateFactor;
    }

    IWICStream* pStream = NULL;
    // Create a WIC stream to map onto the memory.
    hr = m_pIWICFactory->CreateStream(&pStream);
    if (FAILED(hr))
    {
        SetLastMsg(_T("Failed CreateStream, 0x%lx\n"), hr);
        if (m_printMsg)
        {
            _tprintf(m_lastErrMsg);
        }
        return EBitmapLoader::kErrCreateStream;
    }

    // Initialize the stream with the memory pointer and size.
    hr = pStream->InitializeFromMemory(
        reinterpret_cast<BYTE*>(buf),
        len
    );
    if (FAILED(hr))
    {
        SetLastMsg(_T("Failed InitializeFromMemory, 0x%lx\n"), hr);
        if (m_printMsg)
        {
            _tprintf(m_lastErrMsg);
        }
        return EBitmapLoader::kErrStreamInitFromMem;
    }

    // Create a decoder
    IWICBitmapDecoder* pDecoder = NULL;
    RELEASE_AUTO(pDecoder);
    // Create a decoder for the stream.
    hr = m_pIWICFactory->CreateDecoderFromStream(
        pStream,
        NULL,
        WICDecodeMetadataCacheOnLoad,
        &pDecoder
    );
    if (FAILED(hr))
    {
        SetLastMsg(_T("Failed CreateDecoderFromStream, 0x%lx\n"), hr);
        if (m_printMsg)
        {
            _tprintf(m_lastErrMsg);
        }
        return EBitmapLoader::kErrCreateDecoder;
    }

    CHECK_RETURN(CreateDIBFromDecoder(pDecoder, rect));

    return EBitmapLoader::kOk;
}

EBitmapLoader BitmapLoader::CreateDIBFromDecoder(IWICBitmapDecoder* pDecoder, const long rect[4])
{
    HRESULT hr = S_OK;

    // Retrieve the first frame of the image from the decoder
    IWICBitmapFrameDecode* pFrame = NULL;
    RELEASE_AUTO(pFrame);
    hr = pDecoder->GetFrame(0, &pFrame);
    if (FAILED(hr))
    {
        SetLastMsg(_T("Failed GetFrame, 0x%lx\n"), hr);
        if (m_printMsg)
        {
            _tprintf(m_lastErrMsg);
        }
        return EBitmapLoader::kErrGetFrame;
    }

    // Retrieve IWICBitmapSource from the frame
    // m_pOriginalBitmapSource contains the original bitmap and acts as an intermediate
    SafeRelease(m_pOriginalBitmapSource);
    hr = pFrame->QueryInterface(
        IID_IWICBitmapSource,
        reinterpret_cast<void**>(&m_pOriginalBitmapSource));
    if (FAILED(hr))
    {
        SetLastMsg(_T("Failed QueryInterface, 0x%lx\n"), hr);
        if (m_printMsg)
        {
            _tprintf(m_lastErrMsg);
        }
        return EBitmapLoader::kErrQueryFrameInterface;
    }

    // Step 3: Scale the original IWICBitmapSource to the client rect size
    // and convert the pixel format
    IWICBitmapSource* pToRenderBitmapSource = NULL;
    RELEASE_AUTO(pToRenderBitmapSource);
    CHECK_RETURN(ConvertBitmapSource(rect, &pToRenderBitmapSource));

    // Step 4: Create a DIB from the converted IWICBitmapSource
    CHECK_RETURN(CreateDIBSectionFromBitmapSource(pToRenderBitmapSource));

    return EBitmapLoader::kOk;
}

EBitmapLoader BitmapLoader::ConvertBitmapSource(const long rect[4], IWICBitmapSource** ppToRenderBitmapSource)
{
    *ppToRenderBitmapSource = NULL;
    HRESULT hr = S_OK;

    // Create a BitmapScaler
    IWICBitmapScaler* pScaler = NULL;
    RELEASE_AUTO(pScaler);
    hr = m_pIWICFactory->CreateBitmapScaler(&pScaler);
    if (FAILED(hr))
    {
        SetLastMsg(_T("Failed CreateBitmapScaler, 0x%lx\n"), hr);
        if (m_printMsg)
        {
            _tprintf(m_lastErrMsg);
        }
        return EBitmapLoader::kErrCreateBitmapScaler;
    }

    // Initialize the bitmap scaler from the original bitmap map bits
    hr = pScaler->Initialize(
        m_pOriginalBitmapSource,
        rect[2] - rect[0],
        rect[3] - rect[1],
        WICBitmapInterpolationModeFant);
    if (FAILED(hr))
    {
        SetLastMsg(_T("Failed IWICBitmapScaler::Initialize, 0x%lx\n"), hr);
        if (m_printMsg)
        {
            _tprintf(m_lastErrMsg);
        }
        return EBitmapLoader::kErrScalerInit;
    }

    // Format convert the bitmap into 32bppBGR, a convenient 
    // pixel format for GDI+ rendering 
    IWICFormatConverter* pConverter = NULL;
    RELEASE_AUTO(pConverter);
    hr = m_pIWICFactory->CreateFormatConverter(&pConverter);
    if (FAILED(hr))
    {
        SetLastMsg(_T("Failed IWICImagingFactory::CreateFormatConverter, 0x%lx\n"), hr);
        if (m_printMsg)
        {
            _tprintf(m_lastErrMsg);
        }
        return EBitmapLoader::kErrCreateFormatConverter;
    }

    // Format convert to 32bppBGR 
    hr = pConverter->Initialize(
        pScaler,                         // Input bitmap to convert
        GUID_WICPixelFormat32bppBGR,     // Destination pixel format
        WICBitmapDitherTypeNone,         // Specified dither patterm
        NULL,                            // Specify a particular palette 
        0.f,                             // Alpha threshold
        WICBitmapPaletteTypeCustom       // Palette translation type
    );
    if (FAILED(hr))
    {
        SetLastMsg(_T("Failed IWICFormatConverter::Initialize, 0x%lx\n"), hr);
        if (m_printMsg)
        {
            _tprintf(m_lastErrMsg);
        }
        return EBitmapLoader::kErrConverterInit;
    }

    // Store the converted bitmap as ppToRenderBitmapSource 
    hr = pConverter->QueryInterface(
        IID_PPV_ARGS(ppToRenderBitmapSource));
    if (FAILED(hr))
    {
        SetLastMsg(_T("Failed IWICFormatConverter::QueryInterface, 0x%lx\n"), hr);
        if (m_printMsg)
        {
            _tprintf(m_lastErrMsg);
        }
        return EBitmapLoader::kErrConverterQueryInterface;
    }

    return EBitmapLoader::kOk;
}

EBitmapLoader BitmapLoader::CreateDIBSectionFromBitmapSource(IWICBitmapSource* pToRenderBitmapSource)
{
    HRESULT hr = S_OK;

    UINT width = 0;
    UINT height = 0;

    // Check BitmapSource format
    WICPixelFormatGUID pixelFormat;
    hr = pToRenderBitmapSource->GetPixelFormat(&pixelFormat);
    if (FAILED(hr))
    {
        SetLastMsg(_T("Failed IWICBitmapSource::GetPixelFormat, 0x%lx\n"), hr);
        if (m_printMsg)
        {
            _tprintf(m_lastErrMsg);
        }
        return EBitmapLoader::kErrGetPixelFormat;
    }
    if (pixelFormat != GUID_WICPixelFormat32bppBGR)
    {
        return EBitmapLoader::kErrPixelFormatMismatch;
    }
    
    hr = pToRenderBitmapSource->GetSize(&width, &height);
    if (FAILED(hr))
    {
        SetLastMsg(_T("Failed IWICBitmapSource::GetSize, 0x%lx\n"), hr);
        if (m_printMsg)
        {
            _tprintf(m_lastErrMsg);
        }
        return EBitmapLoader::kErrGetPixelSize;
    }
    
    UINT cbStride = 0;
    // Size of a scan line represented in bytes: 4 bytes each pixel
    hr = UIntMult(width, sizeof(Gdiplus::ARGB), &cbStride);
    if (FAILED(hr))
    {
        SetLastMsg(_T("Failed UIntMult, 0x%lx\n"), hr);
        if (m_printMsg)
        {
            _tprintf(m_lastErrMsg);
        }
        return EBitmapLoader::kErrSizeMult;
    }

    UINT cbBufferSize = 0;
    // Size of the image, represented in bytes
    hr = UIntMult(cbStride, height, &cbBufferSize);
    if (FAILED(hr))
    {
        SetLastMsg(_T("Failed UIntMult, 0x%lx\n"), hr);
        if (m_printMsg)
        {
            _tprintf(m_lastErrMsg);
        }
        return EBitmapLoader::kErrSizeMult;
    }

    // Make sure to free previously allocated buffer and bitmap
    DeleteBufferAndBmp();
    m_pbBuffer = new BYTE[cbBufferSize];
    if (!m_pbBuffer)
    {
        SetLastMsg(_T("Failed alloc memory\n"));
        if (m_printMsg)
        {
            _tprintf(m_lastErrMsg);
        }
        return EBitmapLoader::kErrAllocMemory;
    }

    WICRect rc = { 0, 0, static_cast<decltype(rc.Width)>(width), static_cast<decltype(rc.Height)>(height) };
    // Extract the image into the GDI+ Bitmap
    hr = pToRenderBitmapSource->CopyPixels(
        &rc,
        cbStride,
        cbBufferSize,
        m_pbBuffer
    );
    if (FAILED(hr))
    {
        SetLastMsg(_T("Failed IWICBitmapSource::CopyPixels, 0x%lx\n"), hr);
        if (m_printMsg)
        {
            _tprintf(m_lastErrMsg);
        }
        return EBitmapLoader::kErrCopyPixels;
    }

    m_pGdiPlusBitmap = new Gdiplus::Bitmap(
        width,
        height,
        cbStride,
        PixelFormat32bppRGB,
        m_pbBuffer
    );
    if (!m_pGdiPlusBitmap)
    {
        SetLastMsg(_T("Failed alloc memory\n"));
        if (m_printMsg)
        {
            _tprintf(m_lastErrMsg);
        }
        return EBitmapLoader::kErrAllocMemory;
    }

    auto status = m_pGdiPlusBitmap->GetLastStatus();
    if (status != Gdiplus::Ok)
    {
        SetLastMsg(_T("Failed Gdiplus::Bitmap last status %d\n"), static_cast<int>(status));
        if (m_printMsg)
        {
            _tprintf(m_lastErrMsg);
        }

        DeleteBufferAndBmp();
        return EBitmapLoader::kErrBitmapStatus;
    }

    return EBitmapLoader::kOk;
}

void BitmapLoader::DeleteBufferAndBmp()
{
    delete m_pGdiPlusBitmap;
    m_pGdiPlusBitmap = NULL;

    delete[] m_pbBuffer;
    m_pbBuffer = NULL;
}

void BitmapLoader::SetLastMsg(const TCHAR* format, ...)
{
    memset(m_lastErrMsg, 0, sizeof(m_lastErrMsg));
    memcpy_s(
        m_lastErrMsg,
        sizeof(m_lastErrMsg),
        m_msgHeader,
        _tcslen(m_msgHeader) * sizeof(m_msgHeader[0]));

    va_list args;
    va_start(args, format);
    _vsntprintf_s(
        (TCHAR* const)(m_lastErrMsg + _tcslen(m_msgHeader)),
        _countof(m_lastErrMsg) - _tcslen(m_msgHeader),
        _TRUNCATE,
        format,
        args);
    va_end(args);
}
