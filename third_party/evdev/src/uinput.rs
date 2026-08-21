//! Virtual device emulation for evdev via uinput.
//!
//! This is quite useful when testing/debugging devices, or synchronization.

use crate::constants::EventType;
use crate::inputid::{BusType, InputId};
use crate::{
    sys, AttributeSet, AttributeSetRef, InputEvent, Key, LedType, MiscType, RelativeAxisType,
    SwitchType,
};
use libc::O_NONBLOCK;
use std::fs::{File, OpenOptions};
use std::io::{self, Write};
use std::os::unix::{fs::OpenOptionsExt, io::AsRawFd};

const UINPUT_PATH: &str = "/dev/uinput";

#[derive(Debug)]
pub struct VirtualDeviceBuilder<'a> {
    file: File,
    name: &'a [u8],
    id: Option<libc::input_id>,
}

impl<'a> VirtualDeviceBuilder<'a> {
    pub fn new() -> io::Result<Self> {
        let mut options = OpenOptions::new();

        // Open in write-only, in nonblocking mode.
        let file = options
            .write(true)
            .custom_flags(O_NONBLOCK)
            .open(UINPUT_PATH)?;

        Ok(VirtualDeviceBuilder {
            file,
            name: Default::default(),
            id: None,
        })
    }

    #[inline]
    pub fn name<S: AsRef<[u8]> + ?Sized>(mut self, name: &'a S) -> Self {
        self.name = name.as_ref();
        self
    }

    #[inline]
    pub fn input_id(mut self, id: InputId) -> Self {
        self.id = Some(id.0);
        self
    }

    pub fn with_keys(self, keys: &AttributeSetRef<Key>) -> io::Result<Self> {
        // Run ioctls for setting capability bits
        unsafe {
            sys::ui_set_evbit(
                self.file.as_raw_fd(),
                crate::EventType::KEY.0 as nix::sys::ioctl::ioctl_param_type,
            )?;
        }

        for bit in keys.iter() {
            unsafe {
                sys::ui_set_keybit(
                    self.file.as_raw_fd(),
                    bit.0 as nix::sys::ioctl::ioctl_param_type,
                )?;
            }
        }

        Ok(self)
    }

    pub fn with_miscs(self, keys: &AttributeSetRef<MiscType>) -> io::Result<Self> {
        unsafe {
            sys::ui_set_evbit(
                self.file.as_raw_fd(),
                crate::EventType::MISC.0 as nix::sys::ioctl::ioctl_param_type,
            )?;
        }

        for bit in keys.iter() {
            unsafe {
                sys::ui_set_mscbit(
                    self.file.as_raw_fd(),
                    bit.0 as nix::sys::ioctl::ioctl_param_type,
                )?;
            }
        }

        Ok(self)
    }

    pub fn with_leds(self, keys: &AttributeSetRef<LedType>) -> io::Result<Self> {
        unsafe {
            sys::ui_set_evbit(
                self.file.as_raw_fd(),
                crate::EventType::LED.0 as nix::sys::ioctl::ioctl_param_type,
            )?;
        }

        for bit in keys.iter() {
            unsafe {
                sys::ui_set_ledbit(
                    self.file.as_raw_fd(),
                    bit.0 as nix::sys::ioctl::ioctl_param_type,
                )?;
            }
        }

        Ok(self)
    }

    pub fn with_relative_axes(self, axes: &AttributeSetRef<RelativeAxisType>) -> io::Result<Self> {
        unsafe {
            sys::ui_set_evbit(
                self.file.as_raw_fd(),
                crate::EventType::RELATIVE.0 as nix::sys::ioctl::ioctl_param_type,
            )?;
        }

        for bit in axes.iter() {
            unsafe {
                sys::ui_set_relbit(
                    self.file.as_raw_fd(),
                    bit.0 as nix::sys::ioctl::ioctl_param_type,
                )?;
            }
        }

        Ok(self)
    }

    pub fn with_switches(self, switches: &AttributeSetRef<SwitchType>) -> io::Result<Self> {
        unsafe {
            sys::ui_set_evbit(
                self.file.as_raw_fd(),
                crate::EventType::SWITCH.0 as nix::sys::ioctl::ioctl_param_type,
            )?;
        }

        for bit in switches.iter() {
            unsafe {
                sys::ui_set_swbit(
                    self.file.as_raw_fd(),
                    bit.0 as nix::sys::ioctl::ioctl_param_type,
                )?;
            }
        }

        Ok(self)
    }

    pub fn build(self) -> io::Result<VirtualDevice> {
        // Populate the uinput_setup struct

        let mut usetup = libc::uinput_setup {
            id: self.id.unwrap_or(DEFAULT_ID),
            name: [0; libc::UINPUT_MAX_NAME_SIZE],
            ff_effects_max: 0,
        };

        // SAFETY: either casting [u8] to [u8], or [u8] to [i8], which is the same size
        let name_bytes = unsafe { &*(self.name as *const [u8] as *const [libc::c_char]) };
        // Panic if we're doing something really stupid
        // + 1 for the null terminator; usetup.name was zero-initialized so there will be null
        // bytes after the part we copy into
        assert!(name_bytes.len() + 1 < libc::UINPUT_MAX_NAME_SIZE);
        usetup.name[..name_bytes.len()].copy_from_slice(name_bytes);

        VirtualDevice::new(self.file, &usetup)
    }
}

const DEFAULT_ID: libc::input_id = libc::input_id {
    bustype: BusType::BUS_USB.0,
    vendor: 0x1234,  /* sample vendor */
    product: 0x5678, /* sample product */
    version: 0x111,
};

pub struct VirtualDevice {
    file: File,
    file_event: File,
}

impl VirtualDevice {
    /// Create a new virtual device.
    fn new(file: File, usetup: &libc::uinput_setup) -> io::Result<Self> {
        unsafe { sys::ui_dev_setup(file.as_raw_fd(), usetup)? };
        unsafe { sys::ui_dev_create(file.as_raw_fd())? };

        let file_event = Self::open_event_file(&file)?;

        Ok(VirtualDevice { file, file_event })
    }

    fn open_event_file(file: &File) -> io::Result<File> {
        unsafe {
            let mut name = [0u8; 32];
            sys::ui_get_sysname(file.as_raw_fd(), &mut name)?;

            let mut first_nul = name.len() - 1;
            for i in 0..first_nul {
                if name[i] == 0 {
                    first_nul = i;
                    break;
                }
            }

            match std::str::from_utf8(&name[0..first_nul]) {
                Ok(input_name) => {
                    let input_dir = format!("/sys/devices/virtual/input/{}", input_name);
                    let mut readdir = std::fs::read_dir(&input_dir)?;
                    use std::os::unix::ffi::OsStrExt;
                    loop {
                        match readdir.next() {
                            Some(Ok(entry)) => {
                                if let Some(fname) = entry.path().file_name() {
                                    if fname.as_bytes().starts_with(b"event") {
                                        let event_file =
                                            format!("/dev/input/{}", fname.to_string_lossy());
                                        return OpenOptions::new()
                                            .read(true)
                                            // .write(true)
                                            .custom_flags(O_NONBLOCK)
                                            .open(event_file);
                                    }
                                }
                            }
                            None => {
                                return Err(io::Error::new(
                                    io::ErrorKind::NotFound,
                                    format!("Failed to find event of input: {}", &input_dir),
                                ));
                            }
                            Some(Err(_e)) => {
                                // ignore
                            }
                        }
                    }
                }
                Err(e) => Err(io::Error::new(
                    io::ErrorKind::NotFound,
                    format!("Failed to find event, err: {}", e),
                )),
            }
        }
    }

    #[inline]
    fn write_raw(&mut self, messages: &[InputEvent]) -> io::Result<()> {
        let bytes = unsafe { crate::cast_to_bytes(messages) };
        self.file.write_all(bytes)
    }

    /// Post a batch of events to the virtual device.
    ///
    /// The batch is automatically terminated with a `SYN_REPORT` event.
    /// Events from physical devices are batched based on if they occur simultaneously, for example movement
    /// of a mouse triggers a movement events for the X and Y axes separately in a batch of 2 events.
    ///
    /// Single events such as a `KEY` event must still be followed by a `SYN_REPORT`.
    pub fn emit(&mut self, messages: &[InputEvent]) -> io::Result<()> {
        self.write_raw(messages)?;
        let syn = InputEvent::new(EventType::SYNCHRONIZATION, 0, 0);
        self.write_raw(&[syn])
    }

    /// Retrieve the current keypress state directly via kernel syscall.
    #[inline]
    pub fn get_key_state(&self) -> io::Result<AttributeSet<Key>> {
        let mut key_vals = AttributeSet::new();
        self.update_key_state(&mut key_vals)?;
        Ok(key_vals)
    }

    /// Fetch the current kernel key state directly into the provided buffer.
    /// If you don't already have a buffer, you probably want
    /// [`get_key_state`](Self::get_key_state) instead.
    #[inline]
    pub fn update_key_state(&self, key_vals: &mut AttributeSet<Key>) -> io::Result<()> {
        unsafe { sys::eviocgkey(self.file_event.as_raw_fd(), key_vals.as_mut_raw_slice())? };
        Ok(())
    }

    /// Retrieve the current switch state directly via kernel syscall.
    #[inline]
    pub fn get_switch_state(&self) -> io::Result<AttributeSet<SwitchType>> {
        let mut switch_vals = AttributeSet::new();
        self.update_switch_state(&mut switch_vals)?;
        Ok(switch_vals)
    }

    /// Retrieve the current LED state directly via kernel syscall.
    #[inline]
    pub fn get_led_state(&self) -> io::Result<AttributeSet<LedType>> {
        let mut led_vals = AttributeSet::new();
        self.update_led_state(&mut led_vals)?;
        Ok(led_vals)
    }

    /// Fetch the current kernel switch state directly into the provided buffer.
    /// If you don't already have a buffer, you probably want
    /// [`get_switch_state`](Self::get_switch_state) instead.
    #[inline]
    pub fn update_switch_state(
        &self,
        switch_vals: &mut AttributeSet<SwitchType>,
    ) -> io::Result<()> {
        unsafe { sys::eviocgsw(self.file_event.as_raw_fd(), switch_vals.as_mut_raw_slice())? };
        Ok(())
    }

    /// Fetch the current kernel LED state directly into the provided buffer.
    /// If you don't already have a buffer, you probably want
    /// [`get_led_state`](Self::get_led_state) instead.
    #[inline]
    pub fn update_led_state(&self, led_vals: &mut AttributeSet<LedType>) -> io::Result<()> {
        unsafe { sys::eviocgled(self.file_event.as_raw_fd(), led_vals.as_mut_raw_slice())? };
        Ok(())
    }
}
