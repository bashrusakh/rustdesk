use super::{
    ffi::{self, XKeysymToKeycode},
    Context, Error, KeyInfo, PlatformError,
};
use crate::{linux_common, Key};
use std::{os::raw::c_uint};

fn key_event(ctx: &Context, key: Key, down: bool) -> Result<(), Error> {
    unsafe {
        let key_code = (linux_common::to_key_code(key) + 8) as c_uint;
        let press = if down { ffi::True } else { ffi::False };
        if ffi::XTestFakeKeyEvent(ctx.display, key_code, press, ffi::CurrentTime) == 0 {
            return Err(Error::Platform(PlatformError::XTestFakeKeyEvent));
        }
        ffi::XSync(ctx.display, ffi::False);
        Ok(())
    }
}

impl crate::KeyboardContext for Context {
    fn key_down(&mut self, key: Key) -> Result<(), Error> {
        key_event(self, key, true)
    }

    fn key_up(&mut self, key: Key) -> Result<(), Error> {
        key_event(self, key, false)
    }
}

// The implementation of UnicodeKeyboardContext is adapted from here:
// https://github.com/jordansissel/xdotool/blob/master/xdo.c

// TODO: Maybe make this configurable
// The delay is only necessary if the layout is changed. However, inserting a
// delay only at the point where the layout changes doesn't work.
// const KEY_DELAY: Duration = Duration::from_millis(25);

fn info_from_char(ctx: &mut Context, group: u8, ch: char) -> Option<KeyInfo> {
    let key_map: &std::collections::HashMap<char, KeyInfo> = ctx.key_map_vec.get(group as usize)?;
    if let Some(info) = key_map.get(&ch) {
        return Some(*info);
    }

    if ctx.last_group != group {
        ctx.recover_remapped_keycodes();
        ctx.last_group = group;
    }

    let keysym = if ch as u32 >= 0x100 {
        ch as ffi::KeySym + 0x01000000
    } else {
        ch as ffi::KeySym
    };

    unsafe {
        // Checking if the keysym is valid.
        // XKeysymToString returns a pointer to a static string so we're not
        // paying for a memory allocation here.
        if ffi::XKeysymToString(keysym).is_null() {
            return None;
        }
    }

    if let Some(keycode) = ctx.get_remapped_keycode(keysym) {
        Some(KeyInfo {
            keysym,
            group: 0,
            modifiers: 0,
            // keycode: ctx.unused_keycodes,
            keycode,
            default: true,
        })
    } else {
        let keycode = unsafe {
            let keycode = XKeysymToKeycode(ctx.display, keysym);
            // simulate ä will get : in ja, check if the keycode is correct.
            let new_keysym = ffi::XkbKeycodeToKeysym(ctx.display, keycode, group as c_uint, 0);
            if keysym == new_keysym {
                keycode
            } else {
                0
            }
        };
        if keycode != 0 {
            Some(KeyInfo {
                keysym,
                group: 0,
                modifiers: 0,
                // keycode: ctx.unused_keycodes,
                keycode,
                default: true,
            })
        } else if let Ok(keycode) = ctx.remapping(keysym) {
            // This key is not on the default keyboard layout. This means that the
            // unused keycode will be remapped to this keysym.
            Some(KeyInfo {
                keysym,
                group: 0,
                modifiers: 0,
                // keycode: ctx.unused_keycodes,
                keycode,
                default: false,
            })
        } else {
            None
        }
    }
}

unsafe fn modifier_event(ctx: &Context, modifiers: u8, press: ffi::Bool) -> Result<(), Error> {
    // Use the modifier mapping to get the keys associated with a bit in
    // the modifier mask. For each modifier, there may be multiple keys.
    // We press the first non-zero key.

    let key_per_mod = (*ctx.modifier_map).max_keypermod;
    for mod_index in 0..8 {
        if modifiers & (1 << mod_index) == 0 {
            continue;
        }
        for key_index in 0..key_per_mod {
            let index = (mod_index * key_per_mod + key_index) as usize;
            let mut keycode = *(*ctx.modifier_map).modifiermap.add(index);
            // Keycode of altgr is 108 in Rdev
            if keycode == 92 {
                keycode = 108;
            }
            if keycode != 0 {
                if ffi::XTestFakeKeyEvent(ctx.display, keycode as c_uint, press, ffi::CurrentTime)
                    == 0
                {
                    return Err(Error::Platform(PlatformError::XTestFakeKeyEvent));
                }
                ffi::XSync(ctx.display, ffi::False);
                break;
            }
        }
    }

    Ok(())
}

// unsafe fn get_current_modifiers(ctx: &Context) -> Result<u32, Error> {
//     let screen = ffi::XScreenOfDisplay(ctx.display, ctx.screen_number);
//     let window = ffi::XRootWindowOfScreen(screen);
//     // Passing null pointers for the things we don't need results in a
//     // segfault.
//     let mut root_return = ffi::None;
//     let mut child_return = ffi::None;
//     let mut root_x_return = 0;
//     let mut root_y_return = 0;
//     let mut win_x_return = 0;
//     let mut win_y_return = 0;
//     let mut mask_return = 0;
//     if ffi::XQueryPointer(
//         ctx.display,
//         window,
//         &mut root_return,
//         &mut child_return,
//         &mut root_x_return,
//         &mut root_y_return,
//         &mut win_x_return,
//         &mut win_y_return,
//         &mut mask_return,
//     ) == ffi::False
//     {
//         Err(Error::Platform(PlatformError::XQueryPointer))
//     } else {
//         Ok(mask_return)
//     }
// }

unsafe fn key_with_mods_event(ctx: &Context, info: &KeyInfo, down: bool) -> Result<(), Error> {
    // We cannot use XSendEvent here. XSendEvent marks events as fake by
    // setting the send_event property of the XEvent structure. Many
    // applications ignore fake events so we need to use XTestFakeKeyEvent
    // instead.

    // Remember the old group then switch to the new group.

    // TODO
    // // TODO: Need to optimize to improve response speed.
    // let old_modifiers = get_current_modifiers(ctx).unwrap_or(0) as u8;

    // let is_shift = old_modifiers & 1 == 1;   // ShiftMask
    // let is_capslock = old_modifiers & 2 == 2;   // LockMask
    // let is_altgr = old_modifiers & 128 == 128;  // Mod5Mask
    // // Keep modifers is 0
    // if is_capslock && down{
    //     modifier_event(ctx, 2, ffi::True)?;
    //     modifier_event(ctx, 2, ffi::False)?;
    // }
    // if is_shift && down{
    //     modifier_event(ctx, 1, ffi::False)?;
    // }
    // if is_altgr && down{
    //     modifier_event(ctx, 128, ffi::False)?;
    // }

    // Press the modifiers before.
    if info.modifiers != 0 && down {
        modifier_event(ctx, info.modifiers, ffi::True)?;
    }

    let press = if down { ffi::True } else { ffi::False };
    if ffi::XTestFakeKeyEvent(ctx.display, info.keycode as c_uint, press, ffi::CurrentTime) == 0 {
        return Err(Error::Platform(PlatformError::XTestFakeKeyEvent));
    }

    // Release modifiers after.
    if info.modifiers != 0 && !down {
        modifier_event(ctx, info.modifiers, ffi::False)?;
    }

    ffi::XFlush(ctx.display);
    ffi::XSync(ctx.display, ffi::False);

    Ok(())
}

fn char_event(ctx: &mut Context, ch: char, down: bool, up: bool) -> Result<(), Error> {
    let group = unsafe {
        let mut state = std::mem::zeroed();
        ffi::XkbGetState(ctx.display, ffi::XkbUseCoreKbd, &mut state);
        state.group
    };
    let info = match info_from_char(ctx, group, ch) {
        Some(info) => info,
        None => return Err(Error::UnsupportedUnicode(ch)),
    };

    unsafe {
        // let old_group = {
        //     let mut state = std::mem::zeroed();
        //     ffi::XkbGetState(ctx.display, ffi::XkbUseCoreKbd, &mut state);
        //     state.group
        // };

        // if info.group != old_group {
        //     ffi::XkbLockGroup(ctx.display, ffi::XkbUseCoreKbd, info.group as c_uint);
        // }

        if down {
            key_with_mods_event(ctx, &info, true)?;
        }
        if up {
            key_with_mods_event(ctx, &info, false)?;
        }
    }

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

    fn unicode_string(&mut self, _s: &str) -> Result<(), Error> {
        // for ch in s.chars() {
        //     if info_from_char(self, ch).is_none() {
        //         return Err(Error::UnsupportedUnicode(ch));
        //     }
        // }
        // for ch in s.chars() {
        //     self.unicode_char(ch)?;
        // }
        Ok(())
    }
}
