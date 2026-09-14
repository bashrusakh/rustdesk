use impersonate_system;

fn main() {
    loop {
        if let Ok(pid) = impersonate_system::get_process_pid("consent.exe", false) {
            println!("pid:{}", &pid);
        }
        std::thread::sleep(std::time::Duration::from_secs(1));
    }
}
