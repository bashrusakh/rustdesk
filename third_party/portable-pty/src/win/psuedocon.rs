use super::WinChild;
use crate::cmdbuilder::CommandBuilder;
use crate::win::procthreadattr::ProcThreadAttributeList;
use anyhow::{bail, ensure, Error};
use filedescriptor::{FileDescriptor, OwnedHandle};
use lazy_static::lazy_static;
use shared_library::shared_library;
use std::ffi::OsString;
use std::io::Error as IoError;
use std::os::windows::ffi::OsStringExt;
use std::os::windows::io::{AsRawHandle, FromRawHandle};
use std::path::Path;
use std::sync::Mutex;
use std::{mem, ptr};
use winapi::shared::minwindef::DWORD;
use winapi::shared::minwindef::TRUE;
use winapi::shared::winerror::{HRESULT, S_OK};
use winapi::um::handleapi::*;
use winapi::um::processthreadsapi::*;
use winapi::um::userenv::{
    CreateEnvironmentBlock, DestroyEnvironmentBlock, GetUserProfileDirectoryW,
};
use winapi::um::winbase::{
    CREATE_UNICODE_ENVIRONMENT, EXTENDED_STARTUPINFO_PRESENT, STARTF_USESTDHANDLES, STARTUPINFOEXW,
};
use winapi::um::wincon::COORD;
use winapi::um::winnt::HANDLE;

pub type HPCON = HANDLE;

pub const PSEUDOCONSOLE_RESIZE_QUIRK: DWORD = 0x2;
pub const PSEUDOCONSOLE_WIN32_INPUT_MODE: DWORD = 0x4;
#[allow(dead_code)]
pub const PSEUDOCONSOLE_PASSTHROUGH_MODE: DWORD = 0x8;

const UNSUPPORTED_MSG: &str =
    "this system does not support conpty.  Windows 10 October 2018 or newer is required";

shared_library!(ConPtyFuncs,
    pub fn CreatePseudoConsole(
        size: COORD,
        hInput: HANDLE,
        hOutput: HANDLE,
        flags: DWORD,
        hpc: *mut HPCON
    ) -> HRESULT,
    pub fn ResizePseudoConsole(hpc: HPCON, size: COORD) -> HRESULT,
    pub fn ClosePseudoConsole(hpc: HPCON),
);

fn load_conpty() -> Result<ConPtyFuncs, shared_library::LoadingError> {
    // If the kernel doesn't export these functions then their system is
    // too old and we cannot run.
    let kernel = ConPtyFuncs::open(Path::new("kernel32.dll"))?;

    // We prefer to use a sideloaded conpty.dll and openconsole.exe host deployed
    // alongside the application.  We check for this after checking for kernel
    // support so that we don't try to proceed and do something crazy.
    if let Ok(sideloaded) = ConPtyFuncs::open(Path::new("conpty.dll")) {
        Ok(sideloaded)
    } else {
        Ok(kernel)
    }
}

lazy_static! {
    static ref CONPTY: Result<ConPtyFuncs, shared_library::LoadingError> = load_conpty();
}


pub fn check_support() -> Result<(), Error> {
    if CONPTY.is_err() {
        bail!(UNSUPPORTED_MSG);
    }
    Ok(())
}

pub struct PsuedoCon {
    con: HPCON,
}

unsafe impl Send for PsuedoCon {}
unsafe impl Sync for PsuedoCon {}

impl Drop for PsuedoCon {
    fn drop(&mut self) {
        if let Ok(ref funcs) = *CONPTY {
            if self.con != INVALID_HANDLE_VALUE {
                unsafe { (funcs.ClosePseudoConsole)(self.con) };
            }
        }
    }
}

impl PsuedoCon {
    pub fn new(size: COORD, input: FileDescriptor, output: FileDescriptor) -> Result<Self, Error> {
        let conpty = match *CONPTY {
            Ok(ref funcs) => funcs,
            Err(_) => bail!(UNSUPPORTED_MSG),
        };
        let mut con: HPCON = INVALID_HANDLE_VALUE;
        let result = unsafe {
            (conpty.CreatePseudoConsole)(
                size,
                input.as_raw_handle() as _,
                output.as_raw_handle() as _,
                PSEUDOCONSOLE_RESIZE_QUIRK | PSEUDOCONSOLE_WIN32_INPUT_MODE,
                &mut con,
            )
        };
        ensure!(
            result == S_OK,
            "failed to create psuedo console: HRESULT {}",
            result
        );
        Ok(Self { con })
    }

    pub fn resize(&self, size: COORD) -> Result<(), Error> {
        let conpty = match *CONPTY {
            Ok(ref funcs) => funcs,
            Err(_) => bail!(UNSUPPORTED_MSG),
        };
        let result = unsafe { (conpty.ResizePseudoConsole)(self.con, size) };
        ensure!(
            result == S_OK,
            "failed to resize console to {}x{}: HRESULT: {}",
            size.X,
            size.Y,
            result
        );
        Ok(())
    }

    fn get_user_profile_dir(user_token: HANDLE) -> Option<Vec<u16>> {
        let mut len = 0u32;
        unsafe {
            GetUserProfileDirectoryW(user_token, ptr::null_mut(), &mut len);
        }
        if len == 0 {
            return None;
        }
        let mut buf = vec![0u16; len as usize];
        let ok = unsafe { GetUserProfileDirectoryW(user_token, buf.as_mut_ptr(), &mut len) };
        if ok == 0 {
            None
        } else {
            let end = buf.iter().position(|&c| c == 0).unwrap_or(buf.len());
            buf.truncate(end);
            Some(buf)
        }
    }

    pub fn spawn_command(&self, cmd: CommandBuilder) -> anyhow::Result<WinChild> {
        let mut si: STARTUPINFOEXW = unsafe { mem::zeroed() };
        si.StartupInfo.cb = mem::size_of::<STARTUPINFOEXW>() as u32;
        // Explicitly set the stdio handles as invalid handles otherwise
        // we can end up with a weird state where the spawned process can
        // inherit the explicitly redirected output handles from its parent.
        // For example, when daemonizing wezterm-mux-server, the stdio handles
        // are redirected to a log file and the spawned process would end up
        // writing its output there instead of to the pty we just created.
        si.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
        si.StartupInfo.hStdInput = INVALID_HANDLE_VALUE;
        si.StartupInfo.hStdOutput = INVALID_HANDLE_VALUE;
        si.StartupInfo.hStdError = INVALID_HANDLE_VALUE;

        let mut attrs = ProcThreadAttributeList::with_capacity(1)?;
        attrs.set_pty(self.con)?;
        si.lpAttributeList = attrs.as_mut_ptr();

        let mut pi: PROCESS_INFORMATION = unsafe { mem::zeroed() };

        let (mut exe, mut cmdline) = cmd.cmdline()?;
        let cmd_os = OsString::from_wide(&cmdline);

        let cwd = if let Some(user_token) = cmd.get_user_token() {
            Self::get_user_profile_dir(user_token).or_else(|| cmd.current_directory())
        } else {
            cmd.current_directory()
        };

        let creation_flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT;
        let res = if let Some(user_token) = cmd.get_user_token() {
            let mut environment = std::ptr::null_mut();
            unsafe {
                CreateEnvironmentBlock(&mut environment, user_token, TRUE);
                let r = CreateProcessAsUserW(
                    user_token,
                    exe.as_mut_slice().as_mut_ptr(),
                    cmdline.as_mut_slice().as_mut_ptr(),
                    ptr::null_mut(),
                    ptr::null_mut(),
                    0,
                    creation_flags,
                    environment as *mut _,
                    cwd.as_ref()
                        .map(|c| c.as_slice().as_ptr())
                        .unwrap_or(ptr::null()),
                    &mut si.StartupInfo,
                    &mut pi,
                );
                DestroyEnvironmentBlock(environment);
                r
            }
        } else {
            unsafe {
                CreateProcessW(
                    exe.as_mut_slice().as_mut_ptr(),
                    cmdline.as_mut_slice().as_mut_ptr(),
                    ptr::null_mut(),
                    ptr::null_mut(),
                    0,
                    creation_flags,
                    cmd.environment_block().as_mut_slice().as_mut_ptr() as *mut _,
                    cwd.as_ref()
                        .map(|c| c.as_slice().as_ptr())
                        .unwrap_or(ptr::null()),
                    &mut si.StartupInfo,
                    &mut pi,
                )
            }
        };
        if res == 0 {
            let err = IoError::last_os_error();
            let msg = format!(
                "{} `{:?}` in cwd `{:?}` failed: {}",
                if cmd.get_user_token().is_some() {
                    "CreateProcessAsUserW"
                } else {
                    "CreateProcessW"
                },
                cmd_os,
                cwd.as_ref().map(|c| OsString::from_wide(c)),
                err
            );
            log::error!("{}", msg);
            bail!("{}", msg);
        }

        // Make sure we close out the thread handle so we don't leak it;
        // we do this simply by making it owned
        let _main_thread = unsafe { OwnedHandle::from_raw_handle(pi.hThread as _) };
        let proc = unsafe { OwnedHandle::from_raw_handle(pi.hProcess as _) };

        Ok(WinChild {
            proc: Mutex::new(proc),
        })
    }
}
