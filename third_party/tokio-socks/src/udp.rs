use crate::{tcp::*, Error, Result, TargetAddr, ToProxyAddrs};
use bytes::{BufMut, Bytes, BytesMut};
use std::{
    io,
    net::SocketAddr,
    pin::Pin,
    result::Result as StdResult,
    task::{Context, Poll},
};
use tokio::{
    io::{AsyncRead, ReadBuf},
    net::{TcpStream, ToSocketAddrs, UdpSocket},
};
use tokio_util::{
    codec::{Decoder, Encoder},
    udp::UdpFramed,
};

use futures_core::Stream;
use futures_sink::Sink;
use pin_project::pin_project;

#[pin_project]
pub struct Socks5UdpFramed {
    #[pin]
    framed: UdpFramed<Socks5UdpCodec, UdpSocket>,
    #[pin]
    stream: Socks5Stream<TcpStream>,
    socks_addr: SocketAddr,
}

// +----+------+------+----------+----------+----------+
// |RSV | FRAG | ATYP | DST.ADDR | DST.PORT |   DATA   |
// +----+------+------+----------+----------+----------+
// | 2  |  1   |  1   | Variable |    2     | Variable |
// +----+------+------+----------+----------+----------+
#[derive(Debug)]
pub struct Socks5UdpMessage {
    pub rsv: [u8; 2],
    pub frag: u8,
    pub atyp: u8,
    pub dst_addr: TargetAddr<'static>,
    pub data: BytesMut,
}

#[derive(Clone, Debug, Eq, PartialEq, Ord, PartialOrd, Hash, Default)]
pub struct Socks5UdpCodec;

impl Socks5UdpFramed {
    pub async fn connect<P, T>(proxy: P, bind_addr: Option<T>) -> Result<Self>
    where
        P: ToProxyAddrs,
        T: ToSocketAddrs,
    {
        let socket = match bind_addr {
            None => UdpSocket::bind(SocketAddr::from(([0, 0, 0, 0], 0))).await?,
            Some(addr) => UdpSocket::bind(addr).await?,
        };
        let local = socket.local_addr()?;
        let stream = Socks5Stream::associate(proxy, local).await?;
        let framed = UdpFramed::new(socket, Socks5UdpCodec::new());
        let socks_addr = match stream.target_addr() {
            TargetAddr::Ip(addr) => addr,
            _ => unreachable!(),
        };
        Ok(Self {
            framed,
            stream,
            socks_addr,
        })
    }

    pub async fn connect_with_password<'a, P, T>(
        proxy: P,
        bind_addr: Option<T>,
        username: &'a str,
        password: &'a str,
    ) -> Result<Self>
    where
        P: ToProxyAddrs,
        T: ToSocketAddrs,
    {
        let socket = match bind_addr {
            None => UdpSocket::bind(SocketAddr::from(([0, 0, 0, 0], 0))).await?,
            Some(addr) => UdpSocket::bind(addr).await?,
        };
        let local = socket.local_addr()?;
        let stream = Socks5Stream::associate_with_password(proxy, local, username, password).await?;
        let framed = UdpFramed::new(socket, Socks5UdpCodec::new());
        let socks_addr = match stream.target_addr() {
            TargetAddr::Ip(addr) => addr,
            _ => unreachable!(),
        };
        Ok(Self {
            framed,
            stream,
            socks_addr,
        })
    }

    pub fn socks_addr<'a>(&'a self) -> &'a SocketAddr {
        &self.socks_addr
    }

    pub fn local_addr(&self) -> io::Result<SocketAddr> {
        self.framed.get_ref().local_addr()
    }
}

impl Stream for Socks5UdpFramed {
    type Item = StdResult<(<Socks5UdpCodec as Decoder>::Item, SocketAddr), <Socks5UdpCodec as Decoder>::Error>;

    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let this = self.project();
        match this.framed.poll_next(cx) {
            Poll::Ready(d) => return Poll::Ready(d),
            _ => {},
        }

        let mut buf = [0u8; 512];
        let mut buf = ReadBuf::new(&mut buf[..]);
        match this.stream.poll_read(cx, &mut buf) {
            Poll::Ready(Err(e)) => {
                return Poll::Ready(Some(Err(Error::Io(e))));
            },
            Poll::Ready(Ok(())) => {
                // Maybe socks5 server down. Return None.
                return Poll::Ready(None);
            },
            Poll::Pending => {},
        }

        Poll::Pending
    }
}

impl Sink<(Bytes, TargetAddr<'static>)> for Socks5UdpFramed {
    type Error = <Socks5UdpCodec as Encoder<(Bytes, TargetAddr<'static>)>>::Error;

    fn poll_ready(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<StdResult<(), Self::Error>> {
        self.project().framed.poll_ready(cx)
    }

    fn start_send(self: Pin<&mut Self>, item: (Bytes, TargetAddr<'static>)) -> StdResult<(), Self::Error> {
        let send_addr = *self.socks_addr();
        self.project().framed.start_send((item, send_addr))
    }

    #[allow(unused_mut)]
    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<StdResult<(), Self::Error>> {
        self.project().framed.poll_flush(cx)
    }

    fn poll_close(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<StdResult<(), Self::Error>> {
        self.project().framed.poll_close(cx)
    }
}

impl Socks5UdpCodec {
    pub fn new() -> Self {
        Self {}
    }
}

impl Socks5UdpMessage {
    pub fn new() -> Self {
        Self {
            rsv: [0u8; 2],
            frag: 0u8,
            atyp: 0u8,
            dst_addr: TargetAddr::Ip(SocketAddr::from(([0, 0, 0, 0], 0))),
            data: BytesMut::new(),
        }
    }
}

// +----+------+------+----------+----------+----------+
// |RSV | FRAG | ATYP | DST.ADDR | DST.PORT |   DATA   |
// +----+------+------+----------+----------+----------+
// | 2  |  1   |  1   | Variable |    2     | Variable |
// +----+------+------+----------+----------+----------+
impl Decoder for Socks5UdpCodec {
    type Error = Error;
    type Item = Socks5UdpMessage;

    fn decode(&mut self, buf: &mut BytesMut) -> StdResult<Option<Self::Item>, Self::Error> {
        if !buf.is_empty() {
            let buf_len = buf.len();
            if buf_len < 8 {
                return Err(Error::InvalidReservedByte);
            }

            let mut msg = Socks5UdpMessage::new();
            msg.rsv.copy_from_slice(&buf[0..2]);
            if msg.rsv != [0u8, 0u8] {
                return Err(Error::InvalidReservedByte);
            }

            msg.frag = buf[2]; // ignore for now
            msg.atyp = buf[3];

            let mut cursor = 4usize;
            match msg.atyp {
                // IPv4
                0x01 => {
                    if buf_len < 10 {
                        return Err(Error::InvalidTargetAddress("ipv4 address size < 10"));
                    };

                    if 10 == buf_len {
                        buf.clear();
                        return Ok(None);
                    };

                    let mut ip = [0u8; 4];
                    ip.copy_from_slice(&buf[4..8]);
                    let port = u16::from_be_bytes([buf[8], buf[9]]);
                    msg.dst_addr = TargetAddr::Ip(SocketAddr::from((ip, port)));
                    cursor = 10;
                },
                // IPv6
                0x04 => {
                    if buf_len < 22 {
                        return Err(Error::InvalidTargetAddress("ipv6 address size < 22"));
                    };

                    if 22 == buf_len {
                        buf.clear();
                        return Ok(None);
                    };

                    let mut ip = [0u8; 16];
                    ip.copy_from_slice(&buf[4..20]);
                    let port = u16::from_be_bytes([buf[20], buf[21]]);
                    msg.dst_addr = TargetAddr::Ip(SocketAddr::from((ip, port)));
                    cursor = 22;
                },
                // Domain
                0x03 => {
                    let len = buf[4] as usize;
                    if buf_len < 5 + len + 2 {
                        return Err(Error::InvalidTargetAddress("domain address size < target size"));
                    };

                    if 5 + len + 2 == buf_len {
                        buf.clear();
                        return Ok(None);
                    };

                    let domain_bytes = (&buf[5..(len - 2)]).to_vec();
                    let domain = String::from_utf8(domain_bytes)
                        .map_err(|_| Error::InvalidTargetAddress("not a valid UTF-8 string"))?;
                    let port = u16::from_be_bytes([buf[5 + len], buf[5 + len + 1]]);
                    msg.dst_addr = TargetAddr::Domain(domain.into(), port);
                    cursor = 5 + len + 2;
                },

                _ => Err(Error::UnknownAddressType)?,
            }

            msg.data = buf.split_off(cursor);
            buf.clear();
            Ok(Some(msg))
        } else {
            Ok(None)
        }
    }
}

// +----+------+------+----------+----------+----------+
// |RSV | FRAG | ATYP | DST.ADDR | DST.PORT |   DATA   |
// +----+------+------+----------+----------+----------+
// | 2  |  1   |  1   | Variable |    2     | Variable |
// +----+------+------+----------+----------+----------+
impl Encoder<(Bytes, TargetAddr<'static>)> for Socks5UdpCodec {
    type Error = Error;

    // TODO: consider fragment
    fn encode(&mut self, (data, addr): (Bytes, TargetAddr<'static>), buf: &mut BytesMut) -> StdResult<(), Self::Error> {
        let mut header = BytesMut::new();
        header.resize(4, 0u8);

        let mut addr_port = BytesMut::new();
        match addr {
            TargetAddr::Ip(SocketAddr::V4(addr)) => {
                addr_port.reserve(6);
                header[3] = 0x01;
                addr_port.put_slice(&addr.ip().octets());
                addr_port.put_slice(&addr.port().to_be_bytes());
            },
            TargetAddr::Ip(SocketAddr::V6(addr)) => {
                addr_port.reserve(18);
                header[3] = 0x04;
                addr_port.put_slice(&addr.ip().octets());
                addr_port.put_slice(&addr.port().to_be_bytes());
            },
            TargetAddr::Domain(domain, port) => {
                let doman_len = domain.len();
                addr_port.reserve(1 + doman_len + 2);
                header[3] = 0x03;
                addr_port.put_u8(doman_len as u8);
                addr_port.put_slice(domain.as_bytes());
                addr_port.put_slice(&port.to_be_bytes());
            },
        }
        header.extend(addr_port);

        buf.clear();
        buf.extend(header);
        buf.extend(data);

        Ok(())
    }
}
