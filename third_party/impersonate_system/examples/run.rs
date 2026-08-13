use impersonate_system::run_as_system;

// build && run as administrator

fn main() {
    system_account_test();
    arg_test();
}

fn system_account_test() {
    run_as_system("C:\\Windows\\System32\\notepad.exe", "hello.txt").ok();
}

fn arg_test() {
    let sleep = || {
        std::thread::sleep(std::time::Duration::from_millis(100));
    };

    run_as_system("D:\\rust\\tmp\\helloworld.exe", "").ok();
    sleep();
    run_as_system("D:\\rust\\tmp\\h ello\\helloworld.exe", "").ok();
    sleep();
    run_as_system("D:\\rust\\tmp\\你 好\\helloworld.exe", "").ok();
    sleep();
    run_as_system("D:\\rust\\tmp\\你 好\\helloworld.exe", "arg1 你好").ok();
    sleep();
    run_as_system("\"D:\\rust\\tmp\\你 好\\helloworld.exe\"", "arg1 arg2").ok();
    sleep();
    run_as_system("D:/rust/tmp/你 好/helloworld.exe", "arg1 arg2").ok();
    sleep();
    run_as_system("D:/rust/tmp/こ んにちは/helloworld.exe", "arg1 arg2").ok();

    /*
    Args { inner: ["D:\\rust\\tmp\\helloworld.exe"] }
    Args { inner: ["D:\\rust\\tmp\\h ello\\helloworld.exe"] }
    Args { inner: ["D:\\rust\\tmp\\你 好\\helloworld.exe"] }
    Args { inner: ["D:\\rust\\tmp\\你 好\\helloworld.exe", "arg1", "你好"] }
    Args { inner: ["D:\\rust\\tmp\\你 好\\helloworld.exe", "arg1", "arg2"] }
    Args { inner: ["D:/rust/tmp/你 好/helloworld.exe", "arg1", "arg2"] }
    Args { inner: ["D:/rust/tmp/こ んにちは/helloworld.exe", "arg1", "arg2"] }
    */
}
