use anyhow::{anyhow, bail};
use regex::Regex;
use std::process::Command;

use crate::{MacInfo, ResultType};

pub fn get_mac() -> ResultType<MacInfo> {
    let route_output = Command::new("route")
        .args(["-n", "get", "default"])
        .output()?;

    let route_string = String::from_utf8(route_output.stdout)?;

    let interface = route_string
        .lines()
        .find(|line| line.contains("interface"))
        .and_then(|line| line.split_whitespace().last())
        .ok_or(anyhow!("interface not found"))?;

    let mac_output = Command::new("networksetup")
        .args(["-getmacaddress", interface])
        .output()?;

    let mac_string = String::from_utf8(mac_output.stdout)?;

    let addr = {
        let re = Regex::new(
            r"(?i)([0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2})",
        )?;
        let caps = re
            .captures(&mac_string)
            .ok_or(anyhow!("wrong mac format"))?;
        caps.get(1)
            .ok_or(anyhow!("wrong mac format"))?
            .as_str()
            .to_string()
    };

    if !crate::is_valid_mac(&addr) {
        bail!("Invalid MAC address format: {}", addr);
    }

    Ok(MacInfo {
        name: interface.to_string(),
        addr,
    })
}
