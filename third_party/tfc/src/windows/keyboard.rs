#![allow(clippy::field_reassign_with_default)]

use std::{convert::TryInto, ptr::null_mut};

use winapi::{
    shared::minwindef::HKL,
    um::winuser::{
        GetForegroundWindow, GetKeyState, GetKeyboardLayout, GetKeyboardLayoutList,
        GetWindowThreadProcessId, INPUT_u, ToUnicodeEx, VkKeyScanA, VkKeyScanExW, KEYBDINPUT,
    },
};

use super::{
    ffi::{self, VkKeyScanW, DWORD, WORD},
    Context, Error,
};
use crate::Key;

static UNICODE: u16 = 0x0004;
static KEYUP: u16 = 0x0002;
static KEYDOWN: u16 = 0;

fn to_key_code(key: Key) -> ffi::WORD {
    use ffi::*;
    use Key::*;
    match key {
        CapsLock => VK_CAPITAL,
        Shift => VK_LSHIFT,
        Control => VK_LCONTROL,
        Alt => VK_LMENU,
        Meta => VK_LWIN,
        RightShift => VK_RSHIFT,
        RightControl => VK_RCONTROL,
        RightAlt => VK_RMENU,
        RightMeta => VK_RWIN,
        // There is no virtual key code for Fn.
        // https://stackoverflow.com/a/48132748/4093378
        Fn => 0xFF,
        ReturnOrEnter => VK_RETURN,
        Escape => VK_ESCAPE,
        DeleteOrBackspace => VK_BACK,
        ForwardDelete => VK_DELETE,
        Insert => VK_INSERT,
        Tab => VK_TAB,
        Space => VK_SPACE,
        Minus => VK_OEM_MINUS,
        Equal => VK_OEM_PLUS,
        LeftBracket => VK_OEM_4,
        RightBracket => VK_OEM_6,
        Backslash => VK_OEM_5,
        Semicolon => VK_OEM_1,
        Quote => VK_OEM_7,
        Grave => VK_OEM_3,
        Comma => VK_OEM_COMMA,
        Period => VK_OEM_PERIOD,
        Slash => VK_OEM_2,
        UpArrow => VK_UP,
        RightArrow => VK_RIGHT,
        DownArrow => VK_DOWN,
        LeftArrow => VK_LEFT,
        PageUp => VK_PRIOR,
        PageDown => VK_NEXT,
        Home => VK_HOME,
        End => VK_END,
        A => VK_A,
        B => VK_B,
        C => VK_C,
        D => VK_D,
        E => VK_E,
        F => VK_F,
        G => VK_G,
        H => VK_H,
        I => VK_I,
        J => VK_J,
        K => VK_K,
        L => VK_L,
        M => VK_M,
        N => VK_N,
        O => VK_O,
        P => VK_P,
        Q => VK_Q,
        R => VK_R,
        S => VK_S,
        T => VK_T,
        U => VK_U,
        V => VK_V,
        W => VK_W,
        X => VK_X,
        Y => VK_Y,
        Z => VK_Z,
        N0 => VK_0,
        N1 => VK_1,
        N2 => VK_2,
        N3 => VK_3,
        N4 => VK_4,
        N5 => VK_5,
        N6 => VK_6,
        N7 => VK_7,
        N8 => VK_8,
        N9 => VK_9,
        Numpad0 => VK_NUMPAD0,
        Numpad1 => VK_NUMPAD1,
        Numpad2 => VK_NUMPAD2,
        Numpad3 => VK_NUMPAD3,
        Numpad4 => VK_NUMPAD4,
        Numpad5 => VK_NUMPAD5,
        Numpad6 => VK_NUMPAD6,
        Numpad7 => VK_NUMPAD7,
        Numpad8 => VK_NUMPAD8,
        Numpad9 => VK_NUMPAD9,
        // Clear key seems to have no effect.
        NumpadClear => VK_CLEAR,
        NumpadEquals => VK_OEM_PLUS,
        NumpadDivide => VK_DIVIDE,
        NumpadMultiply => VK_MULTIPLY,
        NumpadMinus => VK_OEM_MINUS,
        NumpadPlus => VK_ADD,
        NumpadEnter => VK_RETURN,
        NumpadDecimal => VK_DECIMAL,
        F1 => VK_F1,
        F2 => VK_F2,
        F3 => VK_F3,
        F4 => VK_F4,
        F5 => VK_F5,
        F6 => VK_F6,
        F7 => VK_F7,
        F8 => VK_F8,
        F9 => VK_F9,
        F10 => VK_F10,
        F11 => VK_F11,
        F12 => VK_F12,
        FastForward => VK_MEDIA_NEXT_TRACK,
        Rewind => VK_MEDIA_PREV_TRACK,
        PlayPause => VK_MEDIA_PLAY_PAUSE,
        VolumeUp => VK_VOLUME_UP,
        VolumeDown => VK_VOLUME_DOWN,
        Mute => VK_VOLUME_MUTE,
        _ => VK_A,
    }
}

fn key_event(ctx: &Context, key: Key, down: bool) -> Result<(), Error> {
    let mut input = ffi::INPUT::default();
    input.type_ = ffi::INPUT_KEYBOARD;
    input.u.ki.wVk = to_key_code(key);
    input.u.ki.dwFlags = if down { 0 } else { ffi::KEYEVENTF_KEYUP };
    ctx.send_input(&input)
}

impl crate::KeyboardContext for Context {
    fn key_down(&mut self, key: Key) -> Result<(), Error> {
        key_event(self, key, true)
    }

    fn key_up(&mut self, key: Key) -> Result<(), Error> {
        key_event(self, key, false)
    }

    fn key_click(&mut self, key: Key) -> Result<(), Error> {
        let key_code = to_key_code(key);
        let mut inputs = [ffi::INPUT::default(), ffi::INPUT::default()];
        inputs[0].type_ = ffi::INPUT_KEYBOARD;
        inputs[0].u.ki.wVk = key_code;
        inputs[1].type_ = ffi::INPUT_KEYBOARD;
        inputs[1].u.ki.wVk = key_code;
        inputs[1].u.ki.dwFlags = ffi::KEYEVENTF_KEYUP;
        self.send_inputs(&inputs)
    }
}

fn is_dead(layout: HKL, vk: i32, scan: u16) -> bool {
    const BUF_LEN: i32 = 32;
    let mut buff = [0_u16; BUF_LEN as usize];
    let buff_ptr = buff.as_mut_ptr();
    let mut state = [0; 256];
    let state_ptr = state.as_mut_ptr();
    let len = unsafe {
        ToUnicodeEx(
            vk.try_into().unwrap_or_default(),
            scan.into(),
            state_ptr,
            buff_ptr,
            8 - 1,
            0,
            layout,
        )
    };
    len == -1
}

fn char_event(ctx: &Context, ch: char, down: bool, up: bool) -> Result<(), Error> {
    // send char
    let is_caps = unsafe { GetKeyState(ffi::VK_CAPITAL.into()) < 0 };
    let is_shift = unsafe { GetKeyState(ffi::VK_SHIFT.into()) < 0 };
    let is_alt = unsafe { GetKeyState(ffi::VK_MENU.into()) < 0 };
    let is_control = unsafe { GetKeyState(ffi::VK_CONTROL.into()) < 0 };
    // Keep modifers is 0
    if is_caps && down {
        send_vk(ctx, ffi::VK_CAPITAL.into(), 0, 0, true)?;
        send_vk(ctx, ffi::VK_CAPITAL.into(), 0, 0, false)?;
    }
    if is_shift && down {
        send_vk(ctx, ffi::VK_SHIFT.into(), 0, 0, false)?;
    }
    if is_alt && down {
        send_vk(ctx, ffi::VK_MENU.into(), 0, 0, false)?;
    }
    let mut ch = ch;
    // Ctrl + Shift + F
    if is_shift && is_control && ch.is_uppercase() && down {
        send_vk(ctx, ffi::VK_SHIFT.into(), 0, 0, true)?;
        ch = ch.to_lowercase().collect::<Vec<_>>()[0];
    }
    let layout = unsafe {
        let current_window_thread_id = GetWindowThreadProcessId(GetForegroundWindow(), null_mut());
        GetKeyboardLayout(current_window_thread_id)
    };

    let res = unsafe { VkKeyScanExW(ch as _, layout) };
    let (vk, scan, flags): (i32, u16, u16) = if (res >> 8) & 0xFF == 0 {
        let vk = (res & 0xFF) as i32;
        // Without dead key
        if is_dead(layout, vk, 0) {
            (0, ch as _, UNICODE)
        } else {
            (vk, 0, 0)
        }
    } else {
        (0, ch as _, UNICODE)
    };

    send_vk(ctx, vk, scan, flags, down)?;
    // Ctrl + Shift + F
    if is_shift && is_control && ch.is_lowercase() && down {
        send_vk(ctx, vk, scan, flags, false)?;
    }

    Ok(())
}

fn send_vk(ctx: &Context, vk: i32, scan: u16, flags: u16, down: bool) -> Result<(), Error> {
    let state_flags = if down { KEYDOWN } else { KEYUP };
    let flags: DWORD = (flags | state_flags).into();
    let vk: WORD = vk as _;
    let scan: WORD = scan;

    let mut input = ffi::INPUT::default();
    input.type_ = ffi::INPUT_KEYBOARD;
    input.u.ki.wVk = vk;
    input.u.ki.wScan = scan;
    input.u.ki.dwFlags = flags;

    ctx.send_input(&input)?;

    Ok(())
}

impl crate::UnicodeKeyboardContext for Context {
    fn unicode_char_down(&mut self, ch: char) -> Result<(), Error> {
        char_event(self, ch, true, false)
    }

    fn unicode_char_up(&mut self, ch: char) -> Result<(), Error> {
        char_event(self, ch, false, true)
    }

    fn unicode_char(&mut self, ch: char) -> Result<(), Error> {
        char_event(self, ch, true, true)
    }

    fn unicode_string(&mut self, s: &str) -> Result<(), Error> {
        let mut key_down = ffi::INPUT::default();
        key_down.type_ = ffi::INPUT_KEYBOARD;
        key_down.u.ki.dwFlags = ffi::KEYEVENTF_UNICODE;
        let mut key_up = key_down;
        key_up.u.ki.dwFlags = ffi::KEYEVENTF_UNICODE | ffi::KEYEVENTF_KEYUP;

        let mut inputs = Vec::with_capacity(2 * s.len());
        let mut pair = [0; 2];

        for ch in s.chars() {
            if ch.encode_utf16(&mut pair).len() == 1 {
                key_down.u.ki.wScan = pair[0];
                inputs.push(key_down);
                key_up.u.ki.wScan = pair[0];
                inputs.push(key_up);
            } else {
                key_down.u.ki.wScan = pair[0];
                inputs.push(key_down);
                key_down.u.ki.wScan = pair[1];
                inputs.push(key_down);
                key_up.u.ki.wScan = pair[0];
                inputs.push(key_up);
                key_up.u.ki.wScan = pair[1];
                inputs.push(key_up);
            }
        }

        self.send_inputs(&inputs)
    }
}
