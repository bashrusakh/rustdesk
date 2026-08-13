use cc::Build;

fn main() {
    println!("cargo:rustc-link-lib=advapi32");
    Build::new()
        .define("UNICODE", "1")
        .file("src/source.cpp")
        .compile("impersonate_system");
}
