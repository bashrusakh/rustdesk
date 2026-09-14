mod error;
mod ffi;
mod keyboard;
mod mouse;
mod screen;

// The implementation of Context::new is adapted from here:
// https://github.com/jordansissel/xdotool/blob/master/xdo.c

use crate::linux_x11::ffi::NoSymbol;

use self::ffi::{Display, XKeycodeToKeysym};
use error::PlatformError;
use std::{collections::HashMap, ffi::c_int};
type Error = crate::GenericError<PlatformError>;

#[derive(Copy, Clone, Debug)]
pub struct KeyInfo {
    keysym: ffi::KeySym,
    group: u8,
    modifiers: u8,
    keycode: ffi::KeyCode,
    default: bool,
}

/// The main context used for generating events (Linux-X11).
///
/// The most useful methods are on the [`traits`](crate::traits).
pub struct Context {
    display: *mut ffi::Display,
    screen_number: std::ffi::c_int,
    scroll: crate::linux_common::ScrollAccum,
    pub key_map_vec: Vec<std::collections::HashMap<char, KeyInfo>>,
    unused_keycodes: Vec<ffi::KeyCode>,
    unused_index: u32,
    remap_keysym: HashMap<ffi::KeySym, ffi::KeyCode>,
    modifier_map: *const ffi::XModifierKeymap,
    last_group: u8,
}

unsafe impl Sync for Context {}
unsafe impl Send for Context {}

unsafe fn no_xtest(display: *mut ffi::Display) -> bool {
    // Passing null pointers for the things we don't need results in a
    // segfault.
    let mut event_base = 0;
    let mut error_base = 0;
    let mut major_version = 0;
    let mut minor_version = 0;
    ffi::XTestQueryExtension(
        display,
        &mut event_base,
        &mut error_base,
        &mut major_version,
        &mut minor_version,
    ) == ffi::False
}

unsafe fn find_unused_key_code(
    display: *mut ffi::Display,
    min_keycode: ffi::KeyCode,
    max_keycode: ffi::KeyCode,
) -> Result<Vec<ffi::KeyCode>, Error> {
    // Get the full mapping from keycodes to keysyms. There may be
    // multiple keysyms for each keycode depending on which modifiers
    // are pressed. We need this for finding an unused keycode, that is
    // a keycode without any associated keysyms.
    let mut unused_keycodes = vec![];
    let keycode_count = (max_keycode - min_keycode) + 1;
    let mut keysyms_per_keycode = 0;
    let keysyms = ffi::XGetKeyboardMapping(
        display,
        min_keycode,
        keycode_count as std::ffi::c_int,
        &mut keysyms_per_keycode,
    );
    if keysyms.is_null() {
        return Err(Error::Platform(PlatformError::XGetKeyboardMapping));
    }
    let keysyms_per_keycode = keysyms_per_keycode as usize;

    // Find a keycode that has no keysyms associated with it. This keycode will
    // be used for remapping for the purpose of producing characters that aren't
    // on the default keyboard layout.
    for code_idx in 0..keycode_count {
        let sym_idx = code_idx as usize * keysyms_per_keycode;
        let slice = std::slice::from_raw_parts(keysyms.add(sym_idx), keysyms_per_keycode);
        if slice.iter().all(|keysym| *keysym == ffi::NoSymbol) {
            unused_keycodes.push(min_keycode + code_idx);
        }
    }
    ffi::XFree(keysyms);

    Ok(unused_keycodes)
}

unsafe fn create_key_map(
    display: *mut ffi::Display,
    min_keycode: ffi::KeyCode,
    max_keycode: ffi::KeyCode,
) -> Result<Vec<std::collections::HashMap<char, KeyInfo>>, Error> {
    // Fuck, this library is so inconsistent. Sometimes a keycode is a
    // KeyCode and sometimes it's an int. Sometimes a group is an int
    // and sometimes it's an unsigned int or even an unsigned char.
    // Another thing that bothers me is that this seems to have been
    // designed before const was introduced to C.

    // Anyway, the purpose of the code below is to populate the mapping
    // from character codes to keycodes.
    // The list of keysyms associated with a keycode is divided into
    // groups. Each keysym in a group corresponds to a shift level. When
    // sending the key event, the keycode, the group and the modifier
    // key state identify a single keysym.
    // See https://tronche.com/gui/x/xlib/input/keyboard-encoding.html

    use std::collections::hash_map::{Entry, HashMap};
    use std::ffi::c_uint;

    let desc = ffi::XkbGetMap(display, ffi::XkbAllClientInfoMask, ffi::XkbUseCoreKbd);
    if desc.is_null() {
        return Err(Error::Platform(PlatformError::XkbGetMap));
    }

    ////////////////////////////////////////////////////////////////
    use x11::xlib;
    const XKB_ALL_NAMES_MASK: c_uint = 0x3fff;
    const XKB_ALL_CTRLS_MASK: std::os::raw::c_ulong = 0xF8001FFF;

    let ndisplay = xlib::XOpenDisplay(std::ptr::null());
    let keyboard: xlib::XkbDescPtr = xlib::XkbAllocKeyboard();
    (*keyboard).dpy = ndisplay;
    xlib::XkbGetNames(ndisplay, XKB_ALL_NAMES_MASK, keyboard);
    xlib::XkbGetControls(ndisplay, XKB_ALL_CTRLS_MASK, keyboard);
    let num_groups: u8 = (*(*keyboard).names).groups.len() as u8;
    // to-do: Ensure the comment out the following line is ok.
    // num_groups = num_groups - 1;
    ////////////////////////////////////////////////////////////////
    let mut key_map_vec: Vec<std::collections::HashMap<char, KeyInfo>> =
        Vec::with_capacity(num_groups.into());
    for _i in 0..num_groups.into() {
        key_map_vec.push(HashMap::new());
    }

    for keycode in min_keycode..=max_keycode {
        for group in 0..(num_groups as i32) {
            let key_map = if group < num_groups.into() {
                match key_map_vec.get_mut(group as usize) {
                    Some(key_map) => key_map,
                    // None is unreachable
                    None => return Err(Error::Unknown),
                }
            } else {
                break;
            };
            let key_type = ffi::XkbKeyKeyType(desc, keycode, group);
            for level in 0..(*key_type).num_levels {
                let keysym =
                    ffi::XkbKeycodeToKeysym(display, keycode, group as c_uint, level as c_uint);
                let mut modifiers = 0;

                if !(*key_type).map.is_null()
                    // to-do: Replace to `ptr.is_aligned()`` when upgrading to Rust 1.79
                    && crate::utils::is_aligned((*key_type).map)
                    && (*key_type).map_count as usize <= isize::MAX as usize
                {
                    let maps =
                        std::slice::from_raw_parts((*key_type).map, (*key_type).map_count as usize);
                    for map in maps {
                        if map.active == ffi::True && map.level == level {
                            modifiers = map.mods.mask;
                            break;
                        }
                    }
                }

                let charcode = ffi::xkb_keysym_to_utf32(keysym as ffi::xkb_keysym_t);
                // We only care about keys that yield characters.
                if charcode as u32 == 0 {
                    continue;
                }
                let charcode = match std::char::from_u32(charcode) {
                    Some(c) => c,
                    None => {
                        ffi::XkbFreeClientMap(desc, 0, ffi::True);
                        return Err(Error::Platform(PlatformError::KeySymToUnicode));
                    }
                };
                if let Entry::Vacant(entry) = key_map.entry(charcode) {
                    entry.insert(KeyInfo {
                        keysym,
                        group: group as u8,
                        modifiers,
                        keycode,
                        default: true,
                    });
                }
            }
        }
    }

    ffi::XkbFreeClientMap(desc, 0, ffi::True);

    Ok(key_map_vec)
}

impl Context {
    pub fn new() -> Result<Self, Error> {
        unsafe {
            let display = ffi::XOpenDisplay(std::ptr::null());
            if display.is_null() {
                return Err(Error::Platform(PlatformError::XOpenDisplay));
            }

            if no_xtest(display) {
                ffi::XCloseDisplay(display);
                return Err(Error::Platform(PlatformError::XTestQueryExtension));
            }

            // Get the full range of keycodes used by X11. This is probably
            // always 8-255 on Linux but we should make sure.
            let mut min_keycode = 0;
            let mut max_keycode = 0;
            ffi::XDisplayKeycodes(display, &mut min_keycode, &mut max_keycode);
            let min_keycode = min_keycode as ffi::KeyCode;
            let max_keycode = max_keycode as ffi::KeyCode;

            let unused_keycodes = match find_unused_key_code(display, min_keycode, max_keycode) {
                Ok(k) => k,
                Err(e) => {
                    ffi::XCloseDisplay(display);
                    return Err(e);
                }
            };

            let key_map_vec = match create_key_map(display, min_keycode, max_keycode) {
                Ok(m) => m,
                Err(e) => {
                    ffi::XCloseDisplay(display);
                    return Err(e);
                }
            };

            let modifier_map = ffi::XGetModifierMapping(display);
            if modifier_map.is_null() {
                ffi::XCloseDisplay(display);
                return Err(Error::Platform(PlatformError::XGetModifierMapping));
            }

            Ok(Self {
                display,
                screen_number: ffi::XDefaultScreen(display),
                scroll: Default::default(),
                key_map_vec,
                unused_keycodes,
                unused_index: Default::default(),
                remap_keysym: Default::default(),
                modifier_map,
                last_group: 0,
            })
        }
    }

    pub fn get_unused_keycode(&mut self) -> Option<ffi::KeyCode> {
        if !self.unused_keycodes.is_empty() {
            let index = self.unused_index as usize;
            self.unused_index = (self.unused_index + 1) % self.unused_keycodes.len() as u32;

            Some(self.unused_keycodes[index])
        } else {
            None
        }
    }

    pub fn remapping(&mut self, keysym: ffi::KeySym) -> Result<ffi::KeyCode, Error> {
        let keycode = if let Some(keycode) = self.get_unused_keycode() {
            keycode
        } else {
            return Err(Error::Info("Not found unused keycode".into()));
        };

        change_keyboard_mapping(self.display, keycode, keysym);
        if self.is_valid_remapping(keysym, keycode) {
            self.remap_keysym.insert(keysym, keycode);
            Ok(keycode)
        } else {
            Err(Error::Info("Failed to remap keycode".into()))
        }
    }

    /// Check remapping keycode is valid
    pub fn is_valid_remapping(&self, keysym: ffi::KeySym, keycode: ffi::KeyCode) -> bool {
        let res = unsafe { XKeycodeToKeysym(self.display, keycode, 0) };
        res == keysym
    }

    pub fn get_remapped_keycode(&self, keysym: ffi::KeySym) -> Option<ffi::KeyCode> {
        if let Some(keycode) = self.remap_keysym.get(&keysym).copied() {
            // FIXME: Detect switch input method.
            if self.is_valid_remapping(keysym, keycode) {
                Some(keycode)
            } else {
                None
            }
        } else {
            None
        }
    }

    pub fn recover_remapped_keycodes(&mut self) {
        self.remap_keysym
            .iter()
            .for_each(|(_, keycode)| change_keyboard_mapping(self.display, *keycode, NoSymbol));
        self.remap_keysym.clear();
        self.unused_index = 0;
    }
}

#[inline]
fn change_keyboard_mapping(display: *mut Display, keycode: u8, keysym: ffi::KeySym) {
    unsafe {
        ffi::XChangeKeyboardMapping(display, keycode as c_int, 1, &keysym, 1);
        ffi::XSync(display, ffi::False);
    }
}

impl Drop for Context {
    fn drop(&mut self) {
        unsafe {
            self.recover_remapped_keycodes();
            ffi::XFreeModifiermap(self.modifier_map);
            ffi::XCloseDisplay(self.display);
        }
    }
}

#[test]
fn test_unused_keycode() -> Result<(), Error> {
    let context = Context::new()?;
    dbg!(&context.unused_keycodes);
    Ok(())
}
