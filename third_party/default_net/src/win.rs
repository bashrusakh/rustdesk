use crate::{MacInfo, ResultType};
use anyhow::bail;
use std::{ffi::OsString, mem, os::windows::ffi::OsStringExt, ptr, slice};
use winapi::{
    shared::{
        inaddr::IN_ADDR,
        ntdef::ULONG,
        winerror::{ERROR_SUCCESS, NO_ERROR},
        ws2def::{AF_UNSPEC, SOCKADDR_IN},
    },
    um::{
        iphlpapi::{GetAdaptersAddresses, GetBestInterface},
        iptypes::IP_ADAPTER_ADDRESSES_LH,
    },
};

const GAA_FLAG_NONE: ULONG = 0x0000;
const MAX_ADAPTERS: usize = 1024;

pub fn get_mac() -> ResultType<MacInfo> {
    let if_index = get_if_index()?;
    let adapters = get_adapters()?;
    // Safety: We don't use the pointer after `adapters` is dropped
    let mut ptr = unsafe { adapters.ptr() };
    let mut count = 0;

    unsafe {
        loop {
            if ptr.is_null() || count >= MAX_ADAPTERS {
                break;
            }
            count += 1;

            #[cfg(not(target_pointer_width = "32"))]
            let find = (*ptr).u.s().IfIndex == if_index;
            #[cfg(target_pointer_width = "32")]
            let find = ptr.read_unaligned().u.s().IfIndex == if_index;

            if find {
                let addr = convert_mac_bytes(ptr)?;

                if !crate::is_valid_mac(&addr) {
                    bail!("Invalid MAC address format: {}", addr);
                }

                #[cfg(not(target_pointer_width = "32"))]
                let adapter_name = construct_string((*ptr).FriendlyName)?;
                #[cfg(target_pointer_width = "32")]
                let adapter_name = construct_string(ptr.read_unaligned().FriendlyName)?;
                let name = adapter_name.to_string_lossy().to_string();

                return Ok(MacInfo { name, addr });
            }

            // Otherwise go to the next device
            #[cfg(target_pointer_width = "32")]
            {
                ptr = ptr.read_unaligned().Next;
            }
            #[cfg(not(target_pointer_width = "32"))]
            {
                ptr = (*ptr).Next;
            }
        }
    }

    bail!("no adapter found")
}

fn get_if_index() -> ResultType<u32> {
    // Use 8.8.8.8 (Google DNS) to get the default network adapter
    let sock_addr = SOCKADDR_IN {
        sin_family: AF_UNSPEC as u16,
        sin_port: 0,
        sin_addr: IN_ADDR {
            S_un: unsafe { mem::transmute(0x08080808u32) },
        }, // 8.8.8.8
        sin_zero: [0; 8],
    };
    let mut if_index: u32 = 0;

    // Get the best interface index
    let dst_addr = unsafe { mem::transmute(sock_addr.sin_addr) };
    if unsafe { GetBestInterface(dst_addr, &mut if_index) } == NO_ERROR {
        return Ok(if_index);
    }
    bail!("no adapter found")
}

/// Copy over the 6 MAC address bytes to the buffer and format it as XX:XX:XX:XX:XX:XX.
pub(crate) unsafe fn convert_mac_bytes(ptr: *mut IP_ADAPTER_ADDRESSES_LH) -> ResultType<String> {
    if ptr.is_null() {
        bail!("adapter pointer is null");
    }

    #[cfg(target_pointer_width = "32")]
    let (mac, length) = {
        let adapter = ptr.read_unaligned();
        (adapter.PhysicalAddress, adapter.PhysicalAddressLength)
    };

    #[cfg(not(target_pointer_width = "32"))]
    let (mac, length) = {
        let adapter = &*ptr;
        (adapter.PhysicalAddress, adapter.PhysicalAddressLength)
    };

    if length != 6 {
        bail!("invalid MAC address length");
    }

    Ok(format!(
        "{:02X}:{:02X}:{:02X}:{:02X}:{:02X}:{:02X}",
        mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]
    ))
}

pub(crate) struct AdaptersList {
    ptr: *mut IP_ADAPTER_ADDRESSES_LH,
    size: usize,
}

impl AdaptersList {
    /// Safety: The pointer returned by this method MUST NOT be used after
    /// `self` has gone out of scope. This pointer may also be null.
    pub(crate) unsafe fn ptr(&self) -> *mut IP_ADAPTER_ADDRESSES_LH {
        self.ptr
    }
}

impl Drop for AdaptersList {
    fn drop(&mut self) {
        if !self.ptr.is_null() && self.size != 0 {
            unsafe {
                if let Ok(layout) = std::alloc::Layout::from_size_align(
                    self.size,
                    core::mem::align_of::<IP_ADAPTER_ADDRESSES_LH>(),
                ) {
                    std::alloc::dealloc(self.ptr as *mut u8, layout)
                }
            };
        }
    }
}

pub(crate) fn get_adapters() -> ResultType<AdaptersList> {
    let mut buf_len = 0;

    // This will get the number of bytes we need to allocate for all devices
    unsafe {
        GetAdaptersAddresses(
            AF_UNSPEC as u32,
            GAA_FLAG_NONE,
            ptr::null_mut(),
            ptr::null_mut(),
            &mut buf_len,
        );
    }

    // Add size check to prevent potential OOM
    if buf_len == 0 || buf_len > 1024 * 1024 {  // 1MB limit
        return Ok(AdaptersList {
            ptr: ptr::null_mut(),
            size: 0,
        });
    }

    // Allocate `buf_len` bytes, and create a raw pointer to it with the correct alignment
    // Safety:
    let adapters_list: *mut IP_ADAPTER_ADDRESSES_LH = unsafe {
        std::alloc::alloc(std::alloc::Layout::from_size_align(
            usize::try_from(buf_len).map_err(|e| anyhow::anyhow!(e))?,
            core::mem::align_of::<IP_ADAPTER_ADDRESSES_LH>(),
        )?)
    } as *mut IP_ADAPTER_ADDRESSES_LH;

    // Get our list of adapters
    let result = unsafe {
        GetAdaptersAddresses(
            // [IN] Family
            AF_UNSPEC as u32,
            // [IN] Flags
            GAA_FLAG_NONE,
            // [IN] Reserved
            ptr::null_mut(),
            // [INOUT] AdapterAddresses
            adapters_list,
            // [INOUT] SizePointer
            &mut buf_len,
        )
    };

    let adapters_list = AdaptersList {
        ptr: adapters_list,
        // Cast OK, we checked it above
        size: buf_len as usize,
    };

    // Make sure we were successful
    if result != ERROR_SUCCESS {
        bail!("failed to get adapters");
    }

    Ok(adapters_list)
}

unsafe fn construct_string(ptr: *mut u16) -> ResultType<OsString> {
    if ptr.is_null() {
        bail!("ptr is null");
    }
    let slice = slice::from_raw_parts(ptr, get_null_position(ptr)?);
    Ok(OsStringExt::from_wide(slice))
}

unsafe fn get_null_position(ptr: *mut u16) -> ResultType<usize> {
    if ptr.is_null() {
        bail!("ptr is null");
    }

    const MAX_LENGTH: isize = 2048;
    for i in 0..MAX_LENGTH {
        if ptr.offset(i).is_null() {
            bail!("ptr is null");
        }
        if *ptr.offset(i) == 0 {
            return Ok(i as usize);
        }
    }

    bail!("string too long or no null terminator found")
}
