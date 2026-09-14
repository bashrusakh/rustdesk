//!
//! Definitions for the few things needed from IO Kit
//!

use core_foundation::string::CFStringRef;

#[allow(non_camel_case_types)]
pub type kern_return_t = ::std::os::raw::c_int;

pub type IOReturn = kern_return_t;

pub type IOPMAssertionLevel = u32;

pub type IOPMAssertionID = u32;

#[allow(non_upper_case_globals)]
pub const kIOReturnSuccess: u32 = 0;

#[allow(non_upper_case_globals)]
pub const kIOPMAssertionLevelOn: ::std::os::raw::c_uint = 255;

#[allow(non_upper_case_globals)]
pub const kIOPMAssertionTypePreventUserIdleSystemSleep: &str = "PreventUserIdleSystemSleep";

#[allow(non_upper_case_globals)]
pub const kIOPMAssertionTypePreventUserIdleDisplaySleep: &str = "PreventUserIdleDisplaySleep";

#[allow(non_upper_case_globals)]
pub const kIOPMAssertionTypePreventSystemSleep: &str = "PreventSystemSleep";

extern "C" {
    pub fn IOPMAssertionCreateWithName(
        AssertionType: CFStringRef,
        AssertionLevel: IOPMAssertionLevel,
        AssertionName: CFStringRef,
        AssertionID: *mut IOPMAssertionID,
    ) -> IOReturn;
}

extern "C" {
    pub fn IOPMAssertionRelease(AssertionID: IOPMAssertionID) -> IOReturn;
}
