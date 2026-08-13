use std::borrow::Cow;
use std::io::Read;

use wl_clipboard_rs::{
	copy::{self, Error as CopyError, MimeSource, MimeType, Options, Source},
	paste::{self, get_contents, Error as PasteError, Seat},
	utils::is_primary_selection_supported,
};

use super::encode_as_png;
use super::{into_unknown, LinuxClipboardKind, WaitConfig};
use crate::common::{ClipboardData, ClipboardFormat, Error};
use crate::common::{ImageData, ImageRgba};

const MIME_PNG: &str = "image/png";
const MIME_SVG: &str = "image/svg+xml";
const MIME_HTML: &'static str = "text/html";
const MIME_RTF: &'static str = "text/rtf";
const MIME_URL_LIST: &'static str = "text/uri-list";

pub(crate) struct Clipboard {}

impl TryInto<copy::ClipboardType> for LinuxClipboardKind {
	type Error = Error;

	fn try_into(self) -> Result<copy::ClipboardType, Self::Error> {
		match self {
			LinuxClipboardKind::Clipboard => Ok(copy::ClipboardType::Regular),
			LinuxClipboardKind::Primary => Ok(copy::ClipboardType::Primary),
			LinuxClipboardKind::Secondary => Err(Error::ClipboardNotSupported),
		}
	}
}

impl TryInto<paste::ClipboardType> for LinuxClipboardKind {
	type Error = Error;

	fn try_into(self) -> Result<paste::ClipboardType, Self::Error> {
		match self {
			LinuxClipboardKind::Clipboard => Ok(paste::ClipboardType::Regular),
			LinuxClipboardKind::Primary => Ok(paste::ClipboardType::Primary),
			LinuxClipboardKind::Secondary => Err(Error::ClipboardNotSupported),
		}
	}
}

impl Clipboard {
	#[allow(clippy::unnecessary_wraps)]
	pub(crate) fn new() -> Result<Self, Error> {
		// Check if it's possible to communicate with the wayland compositor
		if let Err(e) = is_primary_selection_supported() {
			return Err(into_unknown("failed to check is_primary_selection_supported", e));
		}
		Ok(Self {})
	}

	fn set_source(
		&self,
		source: MimeSource,
		selection: LinuxClipboardKind,
		wait: WaitConfig,
	) -> Result<(), Error> {
		let mut opts = Options::new();
		opts.foreground(matches!(wait, WaitConfig::Forever));
		opts.clipboard(selection.try_into()?);
		opts.copy(source.source, source.mime_type.clone()).map_err(|e| match e {
			CopyError::PrimarySelectionUnsupported => Error::ClipboardNotSupported,
			other => into_unknown(
				&format!("failed to copy clipboard with {:?}", source.mime_type),
				other,
			),
		})
	}

	fn set_multi_source(
		&self,
		sources: Vec<MimeSource>,
		selection: LinuxClipboardKind,
		wait: WaitConfig,
	) -> Result<(), Error> {
		let mut opts = Options::new();
		opts.foreground(matches!(wait, WaitConfig::Forever));
		opts.clipboard(selection.try_into()?);
		opts.copy_multi(sources).map_err(|e| match e {
			CopyError::PrimarySelectionUnsupported => Error::ClipboardNotSupported,
			other => into_unknown("failed to copy multi sources", other),
		})
	}

	pub(crate) fn get_text(&mut self, selection: LinuxClipboardKind) -> Result<String, Error> {
		self.get_plain(selection, wl_clipboard_rs::paste::MimeType::Text)
	}

	pub(crate) fn set_text(
		&self,
		text: Cow<'_, str>,
		selection: LinuxClipboardKind,
		wait: WaitConfig,
	) -> Result<(), Error> {
		self.set_source(Self::text_to_mime_source(text), selection, wait)
	}

	fn text_to_mime_source(text: Cow<'_, str>) -> MimeSource {
		MimeSource {
			source: Source::Bytes(text.into_owned().into_bytes().into_boxed_slice()),
			mime_type: MimeType::Text,
		}
	}

	pub(crate) fn get_rtf(&mut self, selection: LinuxClipboardKind) -> Result<String, Error> {
		self.get_plain(selection, wl_clipboard_rs::paste::MimeType::Specific(MIME_RTF))
	}

	pub(crate) fn set_rtf(
		&self,
		rtf: Cow<'_, str>,
		selection: LinuxClipboardKind,
		wait: WaitConfig,
	) -> Result<(), Error> {
		self.set_source(Self::rtf_to_mime_source(rtf), selection, wait)
	}

	fn rtf_to_mime_source(rtf: Cow<'_, str>) -> MimeSource {
		MimeSource {
			source: Source::Bytes(rtf.into_owned().into_bytes().into_boxed_slice()),
			mime_type: MimeType::Specific(String::from(MIME_RTF)),
		}
	}

	pub(crate) fn get_html(&mut self, selection: LinuxClipboardKind) -> Result<String, Error> {
		self.get_plain(selection, wl_clipboard_rs::paste::MimeType::Specific(MIME_HTML))
	}

	pub(crate) fn get_url_list(&mut self, selection: LinuxClipboardKind) -> Result<String, Error> {
		self.get_plain(selection, wl_clipboard_rs::paste::MimeType::Specific(&MIME_URL_LIST))
	}

	fn get_plain(
		&mut self,
		selection: LinuxClipboardKind,
		mime_type: wl_clipboard_rs::paste::MimeType,
	) -> Result<String, Error> {
		let result = get_contents(selection.try_into()?, Seat::Unspecified, mime_type);
		match result {
			Ok((mut pipe, _)) => {
				let mut contents = vec![];
				pipe.read_to_end(&mut contents)
					.map_err(|e| into_unknown("failed to read pipe", e))?;
				String::from_utf8(contents)
					.map_err(|e| into_unknown("failed to convert from utf8", e))
			}

			Err(PasteError::ClipboardEmpty) | Err(PasteError::NoMimeType) => {
				Err(Error::ContentNotAvailable)
			}

			Err(PasteError::PrimarySelectionUnsupported) => Err(Error::ClipboardNotSupported),

			Err(err) => Err(Error::Unknown { description: err.to_string() }),
		}
	}

	pub(crate) fn set_html(
		&self,
		html: Cow<'_, str>,
		alt: Option<Cow<'_, str>>,
		selection: LinuxClipboardKind,
		wait: WaitConfig,
	) -> Result<(), Error> {
		let html_source = Self::html_to_mime_source(html);
		match alt {
			Some(alt_text) => self.set_multi_source(
				vec![Self::text_to_mime_source(alt_text), html_source],
				selection,
				wait,
			),
			None => self.set_source(html_source, selection, wait),
		}
	}

	fn html_to_mime_source(html: Cow<'_, str>) -> MimeSource {
		MimeSource {
			source: Source::Bytes(html.into_owned().into_bytes().into_boxed_slice()),
			mime_type: MimeType::Specific(String::from(MIME_HTML)),
		}
	}

	fn url_list_to_mime_source(urls: &[String]) -> MimeSource {
		let urls: Vec<String> = urls.iter().map(|s| super::url::encode_path_to_uri(s)).collect();
		let urls = urls.join("\n");
		MimeSource {
			source: Source::Bytes(urls.into_bytes().into_boxed_slice()),
			mime_type: MimeType::Specific(String::from(MIME_URL_LIST)),
		}
	}

	pub(crate) fn get_image(
		&mut self,
		selection: LinuxClipboardKind,
	) -> Result<ImageData<'static>, Error> {
		match self.get_image_svg(selection) {
			Err(Error::ContentNotAvailable) => self.get_image_png(selection),
			result => result,
		}
	}

	pub(crate) fn get_image_rgba(
		&mut self,
		selection: LinuxClipboardKind,
	) -> Result<ImageData<'static>, Error> {
		use wl_clipboard_rs::paste::MimeType;

		let result =
			get_contents(selection.try_into()?, Seat::Unspecified, MimeType::Specific(MIME_PNG));
		match result {
			Ok((mut pipe, _mime_type)) => {
				let mut buffer = vec![];
				pipe.read_to_end(&mut buffer)
					.map_err(|e| into_unknown("failed to read pipe", e))?;
				let image_data = super::decode_from_png(buffer)?;
				Ok(ImageData::Rgba(image_data))
			}

			Err(PasteError::ClipboardEmpty) | Err(PasteError::NoMimeType) => {
				Err(Error::ContentNotAvailable)
			}

			Err(err) => Err(Error::Unknown { description: err.to_string() }),
		}
	}

	pub(crate) fn get_image_png(
		&mut self,
		selection: LinuxClipboardKind,
	) -> Result<ImageData<'static>, Error> {
		use wl_clipboard_rs::paste::MimeType;

		let result =
			get_contents(selection.try_into()?, Seat::Unspecified, MimeType::Specific(MIME_PNG));
		match result {
			Ok((mut pipe, _mime_type)) => {
				let mut buffer = vec![];
				pipe.read_to_end(&mut buffer)
					.map_err(|e| into_unknown("failed to read pipe", e))?;
				Ok(ImageData::png(buffer.into()))
			}

			Err(PasteError::ClipboardEmpty) | Err(PasteError::NoMimeType) => {
				Err(Error::ContentNotAvailable)
			}

			Err(err) => Err(Error::Unknown { description: err.to_string() }),
		}
	}

	pub(crate) fn get_image_svg(
		&mut self,
		selection: LinuxClipboardKind,
	) -> Result<ImageData<'static>, Error> {
		use wl_clipboard_rs::paste::MimeType;

		let result =
			get_contents(selection.try_into()?, Seat::Unspecified, MimeType::Specific(MIME_SVG));
		match result {
			Ok((mut pipe, _mime_type)) => {
				let mut buffer = vec![];
				pipe.read_to_end(&mut buffer)
					.map_err(|e| into_unknown("failed to read pipe", e))?;
				Ok(ImageData::svg(
					String::from_utf8(buffer)
						.map_err(|e| into_unknown("failed to convert from utf8", e))?,
				))
			}

			Err(PasteError::ClipboardEmpty) | Err(PasteError::NoMimeType) => {
				Err(Error::ContentNotAvailable)
			}

			Err(err) => Err(Error::Unknown { description: err.to_string() }),
		}
	}

	pub(crate) fn set_image(
		&mut self,
		image: ImageData,
		selection: LinuxClipboardKind,
		wait: WaitConfig,
	) -> Result<(), Error> {
		match image {
			ImageData::Rgba(image) => self.set_image_rgba(image, selection, wait),
			ImageData::Png(png) => self.set_image_png(png.to_vec(), selection, wait),
			ImageData::Svg(svg) => self.set_image_svg(svg, selection, wait),
		}
	}

	pub(crate) fn set_image_rgba(
		&mut self,
		image: ImageRgba,
		selection: LinuxClipboardKind,
		wait: WaitConfig,
	) -> Result<(), Error> {
		let image = encode_as_png(&image)?;
		self.set_source(Self::png_to_mime_source(image), selection, wait)
	}

	pub(crate) fn set_image_png(
		&mut self,
		png: Vec<u8>,
		selection: LinuxClipboardKind,
		wait: WaitConfig,
	) -> Result<(), Error> {
		self.set_source(Self::png_to_mime_source(png), selection, wait)
	}

	fn png_to_mime_source(png: Vec<u8>) -> MimeSource {
		MimeSource {
			source: Source::Bytes(png.into_boxed_slice()),
			mime_type: MimeType::Specific(String::from(MIME_PNG)),
		}
	}

	pub(crate) fn set_image_svg(
		&mut self,
		svg: String,
		selection: LinuxClipboardKind,
		wait: WaitConfig,
	) -> Result<(), Error> {
		self.set_source(Self::svg_to_mime_source(svg), selection, wait)
	}

	fn svg_to_mime_source(svg: String) -> MimeSource {
		MimeSource {
			source: Source::Bytes(svg.into_bytes().into_boxed_slice()),
			mime_type: MimeType::Specific(String::from(MIME_SVG)),
		}
	}

	pub(crate) fn get_special(
		&self,
		format_name: &str,
		selection: LinuxClipboardKind,
	) -> Result<Vec<u8>, Error> {
		use wl_clipboard_rs::paste::MimeType;

		let result =
			get_contents(selection.try_into()?, Seat::Unspecified, MimeType::Specific(format_name));
		match result {
			Ok((mut pipe, _mime_type)) => {
				let mut buffer = vec![];
				pipe.read_to_end(&mut buffer)
					.map_err(|e| into_unknown("failed to read pipe", e))?;
				Ok(buffer)
			}

			Err(PasteError::ClipboardEmpty) | Err(PasteError::NoMimeType) => {
				Err(Error::ContentNotAvailable)
			}

			Err(err) => Err(Error::Unknown { description: err.to_string() }),
		}
	}

	pub(crate) fn set_special(
		&self,
		format_name: &str,
		data: &[u8],
		selection: LinuxClipboardKind,
		wait: WaitConfig,
	) -> Result<(), Error> {
		self.set_source(Self::special_to_mime_source(format_name, data), selection, wait)
	}

	fn special_to_mime_source(format_name: &str, data: &[u8]) -> MimeSource {
		MimeSource {
			source: Source::Bytes(data.into()),
			mime_type: MimeType::Specific(String::from(format_name)),
		}
	}

	pub(crate) fn get_formats(
		&mut self,
		formats: &[ClipboardFormat],
		selection: LinuxClipboardKind,
	) -> Result<Vec<ClipboardData>, Error> {
		let mut results = Vec::new();
		let mut err = None;
		let mut err_count = 0;
		for format in formats {
			match format {
				ClipboardFormat::Text => match self.get_text(selection) {
					Ok(text) => results.push(ClipboardData::Text(text)),
					Err(Error::ContentNotAvailable) => results.push(ClipboardData::None),
					Err(e) => {
						log::debug!("Error getting text: {:?}", e);
						results.push(ClipboardData::None);
						err = Some(e);
						err_count += 1;
					}
				},
				ClipboardFormat::Rtf => match self.get_rtf(selection) {
					Ok(rtf) => results.push(ClipboardData::Rtf(rtf)),
					Err(Error::ContentNotAvailable) => results.push(ClipboardData::None),
					Err(e) => {
						log::debug!("Error getting rtf: {:?}", e);
						results.push(ClipboardData::None);
						err = Some(e);
						err_count += 1;
					}
				},
				ClipboardFormat::Html => match self.get_html(selection) {
					Ok(html) => results.push(ClipboardData::Html(html)),
					Err(Error::ContentNotAvailable) => results.push(ClipboardData::None),
					Err(e) => {
						log::debug!("Error getting html: {:?}", e);
						results.push(ClipboardData::None);
						err = Some(e);
						err_count += 1;
					}
				},
				ClipboardFormat::ImageRgba => match self.get_image_rgba(selection) {
					Ok(image) => results.push(ClipboardData::Image(image)),
					Err(Error::ContentNotAvailable) => results.push(ClipboardData::None),
					Err(e) => {
						log::debug!("Error getting image: {:?}", e);
						results.push(ClipboardData::None);
						err = Some(e);
						err_count += 1;
					}
				},
				ClipboardFormat::ImagePng => match self.get_image_png(selection) {
					Ok(image) => results.push(ClipboardData::Image(image)),
					Err(Error::ContentNotAvailable) => results.push(ClipboardData::None),
					Err(e) => {
						log::debug!("Error getting image: {:?}", e);
						results.push(ClipboardData::None);
						err = Some(e);
						err_count += 1;
					}
				},
				ClipboardFormat::ImageSvg => match self.get_image_svg(selection) {
					Ok(image) => results.push(ClipboardData::Image(image)),
					Err(Error::ContentNotAvailable) => results.push(ClipboardData::None),
					Err(e) => {
						log::debug!("Error getting image: {:?}", e);
						results.push(ClipboardData::None);
						err = Some(e);
						err_count += 1;
					}
				},
				ClipboardFormat::FileUrl => match self.get_url_list(selection) {
					Ok(urls) => {
						results.push(ClipboardData::FileUrl(super::url::parse_uri_list(&urls)?))
					}
					Err(Error::ContentNotAvailable) => results.push(ClipboardData::None),
					Err(e) => {
						log::debug!("Error getting url list: {:?}", e);
						results.push(ClipboardData::None);
						err = Some(e);
						err_count += 1;
					}
				},
				ClipboardFormat::Special(format_name) => {
					match self.get_special(format_name, selection) {
						Ok(data) => {
							results.push(ClipboardData::Special((format_name.to_string(), data)))
						}
						Err(Error::ContentNotAvailable) => results.push(ClipboardData::None),
						Err(e) => {
							log::debug!("Error getting special: {:?}", e);
							results.push(ClipboardData::None);
							err = Some(e);
							err_count += 1;
						}
					}
				}
			}
		}
		if err_count == formats.len() {
			if let Some(e) = err {
				Err(e)
			} else {
				// unreachable!() because `err_count == formats.len()`
				Ok(results)
			}
		} else {
			Ok(results)
		}
	}

	pub(crate) fn set_formats(
		&self,
		data: &[ClipboardData],
		selection: LinuxClipboardKind,
		wait: WaitConfig,
	) -> Result<(), Error> {
		let mut sources = Vec::new();
		for item in data {
			match item {
				ClipboardData::Text(text) => {
					sources.push(Self::text_to_mime_source(Cow::Borrowed(text)));
				}
				ClipboardData::Rtf(rtf) => {
					sources.push(Self::rtf_to_mime_source(Cow::Borrowed(rtf)));
				}
				ClipboardData::Html(html) => {
					sources.push(Self::html_to_mime_source(Cow::Borrowed(html)));
				}
				ClipboardData::Image(image) => match image {
					ImageData::Rgba(image) => {
						sources.push(Self::png_to_mime_source(encode_as_png(image)?));
					}
					ImageData::Png(png) => {
						sources.push(Self::png_to_mime_source(png.to_vec()));
					}
					ImageData::Svg(svg) => {
						sources.push(Self::svg_to_mime_source(svg.to_string()));
					}
				},
				ClipboardData::FileUrl(urls) => {
					sources.push(Self::url_list_to_mime_source(urls));
				}
				ClipboardData::Special((format_name, data)) => {
					sources.push(Self::special_to_mime_source(format_name, data));
				}
				_ => {}
			}
		}
		self.set_multi_source(sources, selection, wait)
	}
}
