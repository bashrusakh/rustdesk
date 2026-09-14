use std::os::raw::{c_int, c_void};
use std::os::windows::ffi::OsStrExt;

extern "C" {
    fn RunAsSystem(exe: *const c_void, cmd: *mut c_void) -> c_int;
    fn FindProcessPid(exe: *const c_void, verbose: c_int) -> i64;
}

pub fn run_as_system(exe: &str, arg: &str) -> Result<(), ()> {
    // https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createprocesswithtokenw
    assert!(!exe.is_empty());
    let cmd = if exe.starts_with("\"") {
        format!("{} {}", exe, arg)
    } else {
        format!("\"{}\" {}", exe, arg)
    };
    let mut wcmd = wstring(&cmd);
    unsafe {
        let ccmd = wcmd.as_mut_ptr() as _;
        if 0 == RunAsSystem(std::ptr::null(), ccmd) {
            Ok(())
        } else {
            Err(())
        }
    }
}

pub fn get_process_pid(name: &str, verbose: bool) -> Result<u32, ()> {
    unsafe {
        let wstr = wstring(name);
        let pid = FindProcessPid(wstr.as_ptr() as *const c_void, if verbose { 1 } else { 0 });
        if pid > 0 {
            Ok(pid as _)
        } else {
            Err(())
        }
    }
}

fn wstring(s: &str) -> Vec<u16> {
    std::ffi::OsStr::new(s)
        .encode_wide()
        .chain(Some(0).into_iter())
        .collect()
}
