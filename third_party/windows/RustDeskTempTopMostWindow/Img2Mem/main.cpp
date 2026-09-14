#include <stdio.h>
#include <iostream>
#include <vector>
#include <fstream>  
#include <streambuf>

#include "../WindowInjection/img.h"

void print_usage(const char* exe)
{
    printf("usage: %s <load|save> <img file>\n", exe);
}

int load_img(const char* infile);
int save_img(const char* outfile);

int write_imgdata(const std::vector<char>& data, const char* outfile);

int main(int argc, char* argv[])
{
    if (argc != 3)
    {
        print_usage(argv[0]);
        return 0;
    }

    std::string cmd(argv[1]);
    if (cmd == "load")
    {
        printf("begin load image\n");
        return load_img(argv[2]);
    }
    else if (cmd == "save")
    {
        printf("begin save image\n");
        return save_img(argv[2]);
    }
    else
    {
        print_usage(argv[0]);
        return 0;
    }
}

int write_imgdata(const std::vector<char>& data, const char* outfile)
{
    std::ofstream ofs(outfile, std::ios::trunc);
    if (!ofs.is_open())
    {
        return 1;
    }

    ofs << "#include \"pch.h\"\n";
    ofs << "#include \"./img.h\"\n\n";
    ofs << "const unsigned char g_img[] = {\n";

    auto total = data.size();
    const unsigned char* pdata = reinterpret_cast<const unsigned char*>(data.data());
    
    constexpr auto line_count = 20;
    // 0xaa,_
    char buf[6 * line_count];

    auto c = 0;
    while (c < total)
    {
        memset(buf, 0, sizeof(buf));
        for (long long i = 0; i < line_count && c < total; ++i, ++c)
        {
            _snprintf_s(buf + i * 6, sizeof(buf) - i * 6, _TRUNCATE, "0x%02x, ", pdata[c]);
        }
        ofs << buf;
        ofs << "\n";
    }
    ofs << "};\n\n";

    ofs << "const long long g_imgLen = sizeof(g_img);\n";

    return 0;
}

int load_img(const char* infile)
{
    const char* outfile = "../WindowInjection/img.cpp";

    std::ifstream ifs(infile, std::ios::binary);
    if (!ifs.is_open())
    {
        printf("Failed to open %s\n", infile);
        return 0;
    }

    std::vector<char> imgdata(
        (std::istreambuf_iterator<char>(ifs)),
        std::istreambuf_iterator<char>());

    if (0 != write_imgdata(imgdata, outfile))
    {
        printf("Failed to write image data, %s -> %s\n", infile, outfile);
    }
    else
    {
        printf("Succeeded to write image data, %s -> %s\n", infile, outfile);
    }
    return 0;
}

int save_img(const char* outfile)
{
    std::ofstream ofs(outfile, std::ios::binary|std::ios::trunc);
    if (!ofs.is_open())
    {
        printf("Failed to open %s\n", outfile);
        return 0;
    }

    std::copy(g_img, g_img + g_imgLen, std::ostreambuf_iterator<char>(ofs));
    printf("Succeeded to write to %s\n", outfile);
    return 0;
}
