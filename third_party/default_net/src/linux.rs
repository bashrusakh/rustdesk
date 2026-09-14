use crate::{MacInfo, ResultType};
use anyhow::{anyhow, bail};
use std::fs;
use std::path::PathBuf;

pub fn get_mac() -> ResultType<MacInfo> {
    // Read /proc/net/route to get the default network interface
    let route_content = match fs::read_to_string("/proc/net/route") {
        Ok(content) => content,
        Err(e) => bail!("Failed to read route file: {}", e),
    };

    // Find the network interface with default route
    let default_interface = route_content
        .lines()
        .skip(1) // Skip header line
        .find(|line| {
            let fields: Vec<&str> = line.split_whitespace().collect();
            fields.len() > 1 && fields[1] == "00000000" // Destination address is 0.0.0.0 for default route
        })
        .and_then(|line| line.split_whitespace().next())
        .ok_or_else(|| anyhow!("Could not find default network interface"))?;

    // Build the path to MAC address file
    let mut mac_path = PathBuf::from("/sys/class/net");
    mac_path = mac_path.join(default_interface);
    mac_path = mac_path.join("address");

    if !mac_path.exists() {
        bail!("MAC address file does not exist: {}", mac_path.display());
    }

    // Read and validate MAC address
    let mac = fs::read_to_string(mac_path)
        .map_err(|e| anyhow!("Failed to read MAC address: {}", e))?
        .trim()
        .to_string();

    if !crate::is_valid_mac(&mac) {
        bail!("Invalid MAC address format: {}", mac);
    }

    Ok(MacInfo {
        name: default_interface.to_string(),
        addr: mac,
    })
}
