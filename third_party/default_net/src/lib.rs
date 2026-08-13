#[cfg(target_os = "macos")]
#[path = "macos.rs"]
mod os;
#[cfg(target_os = "windows")]
#[path = "win.rs"]
mod os;
#[cfg(target_os = "linux")]
#[path = "linux.rs"]
mod os;

#[cfg(not(any(target_os = "macos", target_os = "windows", target_os = "linux")))]
#[path = "other.rs"]
mod os;

pub use os::get_mac;

pub type ResultType<F, E = anyhow::Error> = anyhow::Result<F, E>;

#[derive(Debug)]
pub struct MacInfo {
    pub name: String,
    pub addr: String,
}

#[allow(unused)]
pub(crate) fn is_valid_mac(mac: &str) -> bool {
    if mac.len() != 17 {
        return false;
    }
    let bytes: Vec<&str> = mac.split(':').collect();
    if bytes.len() != 6 {
        return false;
    }
    bytes
        .iter()
        .all(|byte| byte.len() == 2 && byte.chars().all(|c| c.is_ascii_hexdigit()))
}

mod test {

    #[test]
    fn test_get_mac() {
        let mac = super::get_mac();
        println!("res: {:?}", mac);
    }
}
