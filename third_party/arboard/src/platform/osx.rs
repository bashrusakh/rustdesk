/*
SPDX-License-Identifier: Apache-2.0 OR MIT

Copyright 2022 The Arboard contributors

The project to which this file belongs is licensed under either of
the Apache 2.0 or the MIT license at the licensee's choice. The terms
and conditions of the chosen license apply to this file.
*/

use crate::{
	common::{into_unknown, Error, ImageData, ImageRgba},
	ClipboardData, ClipboardFormat,
};
use objc2::{
	class, msg_send, msg_send_id,
	rc::{autoreleasepool, Id},
	runtime::ProtocolObject,
	ClassType,
};
use objc2_app_kit::{
	NSPasteboard, NSPasteboardType, NSPasteboardTypeFileURL, NSPasteboardTypeHTML,
	NSPasteboardTypePNG, NSPasteboardTypeRTF, NSPasteboardTypeString, NSPasteboardWriting,
};
use objc2_foundation::{NSArray, NSData, NSString, NSURL};
use std::{
	borrow::Cow,
	os::raw::c_void,
	panic::{RefUnwindSafe, UnwindSafe},
};

const NS_PASTEBOARD_TYPE_SVG: &str = "public.svg-image";

mod url_encode {
	use percent_encoding::AsciiSet;
	const ENCODE_SET: AsciiSet = percent_encoding::CONTROLS.add(b' ').add(b'-').add(b'%');

	pub(super) fn encode_path_to_uri(path: &str) -> String {
		let encoded = percent_encoding::percent_encode(path.as_bytes(), &ENCODE_SET).to_string();
		format!("file://{}", encoded)
	}
}

/// Returns an NSImage object on success.
fn image_from_pixels(
	pixels: Vec<u8>,
	width: usize,
	height: usize,
) -> Result<Id<objc2_app_kit::NSImage>, Box<dyn std::error::Error>> {
	use core_graphics::{
		base::{kCGBitmapByteOrderDefault, kCGImageAlphaLast, kCGRenderingIntentDefault, CGFloat},
		color_space::CGColorSpace,
		data_provider::{CGDataProvider, CustomData},
		image::{CGImage, CGImageRef},
	};
	use objc2_app_kit::NSImage;
	use objc2_foundation::NSSize;
	use std::ffi::c_void;

	#[derive(Debug)]
	struct PixelArray {
		data: Vec<u8>,
	}

	impl CustomData for PixelArray {
		unsafe fn ptr(&self) -> *const u8 {
			self.data.as_ptr()
		}
		unsafe fn len(&self) -> usize {
			self.data.len()
		}
	}

	let colorspace = CGColorSpace::create_device_rgb();
	let pixel_data: Box<Box<dyn CustomData>> = Box::new(Box::new(PixelArray { data: pixels }));
	let provider = unsafe { CGDataProvider::from_custom_data(pixel_data) };

	let cg_image = CGImage::new(
		width,
		height,
		8,
		32,
		4 * width,
		&colorspace,
		kCGBitmapByteOrderDefault | kCGImageAlphaLast,
		&provider,
		false,
		kCGRenderingIntentDefault,
	);

	// Convert the owned `CGImage` into a reference `&CGImageRef`, and pass
	// that as `*const c_void`, since `CGImageRef` does not implement
	// `RefEncode`.
	let cg_image: *const CGImageRef = &*cg_image;
	let cg_image: *const c_void = cg_image.cast();

	let size = NSSize { width: width as CGFloat, height: height as CGFloat };
	// XXX: Use `NSImage::initWithCGImage_size` once `objc2-app-kit` supports
	// CoreGraphics.
	let image: Id<NSImage> =
		unsafe { msg_send_id![NSImage::alloc(), initWithCGImage: cg_image, size:size] };

	Ok(image)
}

pub(crate) struct Clipboard {
	pasteboard: Id<NSPasteboard>,
}

unsafe impl Send for Clipboard {}
unsafe impl Sync for Clipboard {}
impl UnwindSafe for Clipboard {}
impl RefUnwindSafe for Clipboard {}

impl Clipboard {
	pub(crate) fn new() -> Result<Clipboard, Error> {
		// Rust only supports 10.7+, while `generalPasteboard` first appeared
		// in 10.0, so this should always be available.
		//
		// However, in some edge cases, like running under launchd (in some
		// modes) as a daemon, the clipboard object may be unavailable, and
		// then `generalPasteboard` will return NULL even though it's
		// documented not to.
		//
		// Otherwise we'd just use `NSPasteboard::generalPasteboard()` here.
		let pasteboard: Option<Id<NSPasteboard>> =
			unsafe { msg_send_id![NSPasteboard::class(), generalPasteboard] };

		if let Some(pasteboard) = pasteboard {
			Ok(Clipboard { pasteboard })
		} else {
			Err(Error::ClipboardNotSupported)
		}
	}

	fn clear(&mut self) {
		unsafe { self.pasteboard.clearContents() };
	}

	// fn get_binary_contents(&mut self) -> Result<Option<ClipboardContent>, Box<dyn std::error::Error>> {
	// 	let string_class: Id<NSObject> = {
	// 		let cls: Id<Class> = unsafe { Id::from_ptr(class("NSString")) };
	// 		unsafe { transmute(cls) }
	// 	};
	// 	let image_class: Id<NSObject> = {
	// 		let cls: Id<Class> = unsafe { Id::from_ptr(class("NSImage")) };
	// 		unsafe { transmute(cls) }
	// 	};
	// 	let url_class: Id<NSObject> = {
	// 		let cls: Id<Class> = unsafe { Id::from_ptr(class("NSURL")) };
	// 		unsafe { transmute(cls) }
	// 	};
	// 	let classes = vec![url_class, image_class, string_class];
	// 	let classes: Id<NSArray<NSObject, Owned>> = NSArray::from_vec(classes);
	// 	let options: Id<NSDictionary<NSObject, NSObject>> = NSDictionary::new();
	// 	let contents: Id<NSArray<NSObject>> = unsafe {
	// 		let obj: *mut NSArray<NSObject> =
	// 			msg_send![self.pasteboard, readObjectsForClasses:&*classes options:&*options];
	// 		if obj.is_null() {
	// 			return Err(err("pasteboard#readObjectsForClasses:options: returned null"));
	// 		}
	// 		Id::from_ptr(obj)
	// 	};
	// 	if contents.count() == 0 {
	// 		Ok(None)
	// 	} else {
	// 		let obj = &contents[0];
	// 		if obj.is_kind_of(Class::get("NSString").unwrap()) {
	// 			let s: &NSString = unsafe { transmute(obj) };
	// 			Ok(Some(ClipboardContent::Utf8(s.as_str().to_owned())))
	// 		} else if obj.is_kind_of(Class::get("NSImage").unwrap()) {
	// 			let tiff: &NSArray<NSObject> = unsafe { msg_send![obj, TIFFRepresentation] };
	// 			let len: usize = unsafe { msg_send![tiff, length] };
	// 			let bytes: *const u8 = unsafe { msg_send![tiff, bytes] };
	// 			let vec = unsafe { std::slice::from_raw_parts(bytes, len) };
	// 			// Here we copy the entire &[u8] into a new owned `Vec`
	// 			// Is there another way that doesn't copy multiple megabytes?
	// 			Ok(Some(ClipboardContent::Tiff(vec.into())))
	// 		} else if obj.is_kind_of(Class::get("NSURL").unwrap()) {
	// 			let s: &NSString = unsafe { msg_send![obj, absoluteString] };
	// 			Ok(Some(ClipboardContent::Utf8(s.as_str().to_owned())))
	// 		} else {
	// 			// let cls: &Class = unsafe { msg_send![obj, class] };
	// 			// println!("{}", cls.name());
	// 			Err(err("pasteboard#readObjectsForClasses:options: returned unknown class"))
	// 		}
	// 	}
	// }
}

pub(crate) struct Get<'clipboard> {
	clipboard: &'clipboard Clipboard,
}

impl<'clipboard> Get<'clipboard> {
	pub(crate) fn new(clipboard: &'clipboard mut Clipboard) -> Self {
		Self { clipboard }
	}

	#[inline]
	pub(crate) fn text(self) -> Result<String, Error> {
		unsafe { self.plain(NSPasteboardTypeString) }
	}

	#[inline]
	pub(crate) fn rtf(self) -> Result<String, Error> {
		unsafe { self.plain(NSPasteboardTypeRTF) }
	}

	#[inline]
	pub(crate) fn html(self) -> Result<String, Error> {
		unsafe { self.plain(NSPasteboardTypeHTML) }
	}

	fn plain(self, r#type: &NSPasteboardType) -> Result<String, Error> {
		// XXX: There does not appear to be an alternative for obtaining text without the need for
		// autorelease behavior.
		autoreleasepool(|_| {
			// XXX: We explicitly use `pasteboardItems` and not `stringForType` since the latter will concat
			// multiple strings, if present, into one and return it instead of reading just the first which is `arboard`'s
			// historical behavior.
			let contents =
				unsafe { self.clipboard.pasteboard.pasteboardItems() }.ok_or_else(|| {
					Error::Unknown {
						description: String::from("NSPasteboard#pasteboardItems errored"),
					}
				})?;

			for item in contents {
				if let Some(string) = unsafe { item.stringForType(r#type) } {
					return Ok(string.to_string());
				}
			}

			Err(Error::ContentNotAvailable)
		})
	}

	pub(crate) fn image(self) -> Result<ImageData<'static>, Error> {
		match self.image_svg() {
			Err(Error::ContentNotAvailable) => match self.image_png() {
				Ok(image) => Ok(image),
				Err(Error::ContentNotAvailable) => self.image_tiff(),
				Err(e) => Err(e),
			},
			result => result,
		}
	}

	fn image_tiff(&self) -> Result<ImageData<'static>, Error> {
		use objc2_app_kit::NSPasteboardTypeTIFF;
		use std::io::Cursor;

		// XXX: There does not appear to be an alternative for obtaining images without the need for
		// autorelease behavior.
		let image = autoreleasepool(|_| {
			let image_data = unsafe { self.clipboard.pasteboard.dataForType(NSPasteboardTypeTIFF) }
				.ok_or(Error::ContentNotAvailable)?;

			let data = Cursor::new(image_data.bytes());

			let reader = image::io::Reader::with_format(data, image::ImageFormat::Tiff);
			reader.decode().map_err(|e| into_unknown("failed to decode tiff", e))
		})?;

		let rgba = image.into_rgba8();
		let (width, height) = rgba.dimensions();

		Ok(ImageData::rgba(width as _, height as _, rgba.into_raw().into()))
	}

	fn image_png(&self) -> Result<ImageData<'static>, Error> {
		autoreleasepool(|_| {
			let image_data = unsafe { self.clipboard.pasteboard.dataForType(NSPasteboardTypePNG) }
				.ok_or(Error::ContentNotAvailable)?;
			Ok(ImageData::png(image_data.bytes().to_owned().into()))
		})
	}

	fn image_svg(&self) -> Result<ImageData<'static>, Error> {
		autoreleasepool(|_| {
			let image_data = unsafe {
				self.clipboard.pasteboard.stringForType(&NSString::from_str(NS_PASTEBOARD_TYPE_SVG))
			}
			.ok_or(Error::ContentNotAvailable)?;
			Ok(ImageData::Svg(image_data.to_string()))
		})
	}

	pub(crate) fn special(self, format_name: &str) -> Result<Vec<u8>, Error> {
		autoreleasepool(|_| {
			let contents =
				unsafe { self.clipboard.pasteboard.pasteboardItems() }.ok_or_else(|| {
					Error::Unknown {
						description: String::from("NSPasteboard#pasteboardItems errored"),
					}
				})?;

			for item in contents {
				if let Some(data) = unsafe { item.dataForType(&NSString::from_str(format_name)) } {
					return Ok(data.bytes().to_vec());
				}
			}

			Err(Error::ContentNotAvailable)
		})
	}

	pub(crate) fn formats(self, formats: &[ClipboardFormat]) -> Result<Vec<ClipboardData>, Error> {
		autoreleasepool(|_| {
			let contents =
				unsafe { self.clipboard.pasteboard.pasteboardItems() }.ok_or_else(|| {
					Error::Unknown {
						description: String::from("NSPasteboard#pasteboardItems errored"),
					}
				})?;

			let mut results = Vec::new();
			for format in formats {
				let pre_size = results.len();
				let mut file_urls = Vec::new();
				for item in contents.iter() {
					match format {
						ClipboardFormat::Text => {
							if let Some(string) =
								unsafe { item.stringForType(NSPasteboardTypeString) }
							{
								results.push(ClipboardData::Text(string.to_string()));
								break;
							}
						}
						ClipboardFormat::Rtf => {
							if let Some(string) = unsafe { item.stringForType(NSPasteboardTypeRTF) }
							{
								results.push(ClipboardData::Rtf(string.to_string()));
								break;
							}
						}
						ClipboardFormat::Html => {
							if let Some(string) =
								unsafe { item.stringForType(NSPasteboardTypeHTML) }
							{
								results.push(ClipboardData::Html(string.to_string()));
								break;
							}
						}
						ClipboardFormat::ImageRgba => match self.image_tiff() {
							Ok(image) => {
								results.push(ClipboardData::Image(image));
								break;
							}
							Err(Error::ContentNotAvailable) => {}
							Err(e) => {
								log::debug!("Error reading image: {:?}", e);
								break;
							}
						},
						ClipboardFormat::ImagePng => match self.image_png() {
							Ok(image) => {
								results.push(ClipboardData::Image(image));
								break;
							}
							Err(Error::ContentNotAvailable) => {}
							Err(e) => {
								log::debug!("Error reading image: {:?}", e);
								break;
							}
						},
						ClipboardFormat::ImageSvg => match self.image_svg() {
							Ok(image) => {
								results.push(ClipboardData::Image(image));
								break;
							}
							Err(Error::ContentNotAvailable) => {}
							Err(e) => {
								log::debug!("Error reading image: {:?}", e);
								break;
							}
						},
						ClipboardFormat::FileUrl => unsafe {
							if let Some(urls) = item.stringForType(NSPasteboardTypeFileURL) {
								let Some(urls) = NSURL::URLWithString(&urls) else {
									log::debug!("Error converting to NSURL");
									break;
								};
								if let Some(path) = urls.path() {
									file_urls.push(path.to_string());
								}
							}
						},
						ClipboardFormat::Special(format_name) => {
							if let Some(data) =
								unsafe { item.dataForType(&NSString::from_str(format_name)) }
							{
								results.push(ClipboardData::Special((
									format_name.to_string(),
									data.bytes().to_vec(),
								)));
								break;
							}
						}
					}
				}
				if !file_urls.is_empty() {
					results.push(ClipboardData::FileUrl(file_urls));
				}

				if results.len() == pre_size {
					results.push(ClipboardData::None);
				}
			}
			Ok(results)
		})
	}
}

pub(crate) struct Set<'clipboard> {
	clipboard: &'clipboard mut Clipboard,
}

impl<'clipboard> Set<'clipboard> {
	pub(crate) fn new(clipboard: &'clipboard mut Clipboard) -> Self {
		Self { clipboard }
	}

	pub(crate) fn text(mut self, data: Cow<'_, str>) -> Result<(), Error> {
		self.text_(data, true)
	}

	fn text_(&mut self, data: Cow<'_, str>, clear: bool) -> Result<(), Error> {
		if clear {
			self.clipboard.clear();
		}
		let string_array =
			NSArray::from_vec(vec![ProtocolObject::from_id(NSString::from_str(&data))]);
		let success = unsafe { self.clipboard.pasteboard.writeObjects(&string_array) };
		if success {
			Ok(())
		} else {
			Err(Error::Unknown { description: "NSPasteboard#writeObjects: returned false".into() })
		}
	}

	pub(crate) fn rtf(mut self, data: Cow<'_, str>) -> Result<(), Error> {
		self.rtf_(data, true)
	}

	fn rtf_(&mut self, data: Cow<'_, str>, clear: bool) -> Result<(), Error> {
		if clear {
			self.clipboard.clear();
		}
		let success = unsafe {
			self.clipboard
				.pasteboard
				.setString_forType(&NSString::from_str(&data), NSPasteboardTypeRTF)
		};
		if success {
			Ok(())
		} else {
			Err(Error::Unknown {
				description: "NSPasteboard#setString_forType: returned false".into(),
			})
		}
	}

	pub(crate) fn html(
		mut self,
		html: Cow<'_, str>,
		alt: Option<Cow<'_, str>>,
	) -> Result<(), Error> {
		self.html_(html, alt, true)
	}

	fn try_wrap_html(html: Cow<'_, str>) -> Id<NSString> {
		// Text goes to the clipboard as UTF-8 but may be interpreted as Windows Latin 1.
		// This wrapping forces it to be interpreted as UTF-8.
		//
		// See:
		// https://bugzilla.mozilla.org/show_bug.cgi?id=466599
		// https://bugs.chromium.org/p/chromium/issues/detail?id=11957
		let wrap_prefix = r#"<html><head><meta http-equiv="content-type" content="text/html; charset=utf-8"></head><body>"#;
		let wrap_suffix = "</body></html>";
		if html.starts_with(wrap_prefix) {
			NSString::from_str(&html)
		} else {
			let html = format!("{wrap_prefix}{html}{wrap_suffix}",);
			NSString::from_str(&html)
		}
	}

	fn html_(
		&mut self,
		html: Cow<'_, str>,
		alt: Option<Cow<'_, str>>,
		clear: bool,
	) -> Result<(), Error> {
		if clear {
			self.clipboard.clear();
		}
		let html_nss = Self::try_wrap_html(html);
		// Make sure that we pass a pointer to the string and not the object itself.
		let mut success =
			unsafe { self.clipboard.pasteboard.setString_forType(&html_nss, NSPasteboardTypeHTML) };
		if success {
			if let Some(alt_text) = alt {
				let alt_nss = NSString::from_str(&alt_text);
				// Similar to the primary string, we only want a pointer here too.
				success = unsafe {
					self.clipboard.pasteboard.setString_forType(&alt_nss, NSPasteboardTypeString)
				};
			}
		}
		if success {
			Ok(())
		} else {
			Err(Error::Unknown {
				description: "NSPasteboard#setString_forType: returned false".into(),
			})
		}
	}

	pub(crate) fn image(mut self, data: ImageData) -> Result<(), Error> {
		self.image_(data, true)
	}

	fn image_(&mut self, data: ImageData, clear: bool) -> Result<(), Error> {
		match data {
			ImageData::Rgba(data) => self.image_pixels(data, clear),
			ImageData::Png(data) => self.image_png(&data, clear),
			ImageData::Svg(data) => self.image_svg(data, clear),
		}
	}

	pub(crate) fn image_pixels(&mut self, data: ImageRgba, clear: bool) -> Result<(), Error> {
		if clear {
			self.clipboard.clear();
		}

		let pixels = data.bytes.into();
		let image = image_from_pixels(pixels, data.width, data.height)
			.map_err(|e| into_unknown("failed to get image from pixels", e))?;

		let image_array = NSArray::from_vec(vec![ProtocolObject::from_id(image)]);
		let success = unsafe { self.clipboard.pasteboard.writeObjects(&image_array) };
		if success {
			Ok(())
		} else {
			Err(Error::Unknown {
				description:
					"Failed to write the image to the pasteboard (`writeObjects` returned NO)."
						.into(),
			})
		}
	}

	pub(crate) fn image_png(&mut self, data: &[u8], clear: bool) -> Result<(), Error> {
		if clear {
			self.clipboard.clear();
		}

		autoreleasepool(|_| {
			let success = unsafe {
				let nsdata: *const objc2_foundation::NSData = msg_send![class!(NSData), dataWithBytes:data.as_ptr() as *const c_void length:data.len() as u64];
				if nsdata.is_null() {
					return Err(Error::Unknown {
						description: "Failed to create NSData from bytes".into(),
					});
				}

				self.clipboard
					.pasteboard
					.setData_forType(Some(&*(nsdata as *const NSData)), NSPasteboardTypePNG)
			};

			if success {
				Ok(())
			} else {
				Err(Error::Unknown {
					description: "Failed to write the PNG image to the pasteboard.".into(),
				})
			}
		})
	}

	pub(crate) fn image_svg(&mut self, data: String, clear: bool) -> Result<(), Error> {
		if clear {
			self.clipboard.clear();
		}

		let svg = NSString::from_str(&data);
		let success = unsafe {
			self.clipboard
				.pasteboard
				.setString_forType(&svg, &NSString::from_str(NS_PASTEBOARD_TYPE_SVG))
		};
		if success {
			Ok(())
		} else {
			Err(Error::Unknown {
				description: "Failed to write the SVG image to the pasteboard.".into(),
			})
		}
	}

	pub(crate) fn special(mut self, format_name: &str, data: &[u8]) -> Result<(), Error> {
		self.special_(format_name, data, true)
	}

	fn special_(&mut self, format_name: &str, data: &[u8], clear: bool) -> Result<(), Error> {
		if clear {
			self.clipboard.clear();
		}
		autoreleasepool(|_| {
			let success = unsafe {
				let nsdata: *const objc2_foundation::NSData = msg_send![class!(NSData), dataWithBytes:data.as_ptr() as *const c_void length:data.len() as u64];
				if nsdata.is_null() {
					return Err(Error::Unknown {
						description: "Failed to create NSData from bytes".into(),
					});
				}

				self.clipboard.pasteboard.setData_forType(
					Some(&*(nsdata as *const NSData)),
					&NSString::from_str(format_name),
				)
			};
			if success {
				Ok(())
			} else {
				Err(Error::Unknown {
					description: "NSPasteboard#setData_forType: returned false".into(),
				})
			}
		})
	}

	pub(crate) fn formats(self, data: &[ClipboardData]) -> Result<(), Error> {
		self.clipboard.clear();

		autoreleasepool(|_| unsafe {
			// Use a single NSPasteboardItem for text-based formats (Text, Rtf, Html, Special)
			// so they are treated as multiple representations of the same content,
			// not as separate items that would all be pasted.
			//
			// Design note: Special data (e.g., owner marker) is placed in main_item along with
			// text formats, while Image/FileUrl use separate items. Ideally Special should be
			// in the same item as Image/FileUrl too, but this doesn't affect functionality
			// since RustDesk iterates all items when reading. Can be optimized later if needed.
			let main_item = objc2_app_kit::NSPasteboardItem::new();
			let mut has_main_item_data = false;

			let mut write_objects: Vec<Id<ProtocolObject<(dyn NSPasteboardWriting + 'static)>>> =
				vec![];

			for d in data {
				match d {
					// Text-based formats go into the main_item as different representations
					ClipboardData::Text(text) => {
						main_item
							.setString_forType(&NSString::from_str(&text), NSPasteboardTypeString);
						has_main_item_data = true;
					}
					ClipboardData::Rtf(rtf) => {
						main_item.setString_forType(&NSString::from_str(&rtf), NSPasteboardTypeRTF);
						has_main_item_data = true;
					}
					ClipboardData::Html(html) => {
						main_item
							.setString_forType(&NSString::from_str(&html), NSPasteboardTypeHTML);
						has_main_item_data = true;
					}
					// Image and FileUrl use separate items as they require different object types
					ClipboardData::Image(data) => match data {
						ImageData::Rgba(data) => {
							let pixels = data.bytes.clone().into();
							let image = image_from_pixels(pixels, data.width, data.height)
								.map_err(|e| into_unknown("failed to get rgba from pixels", e))?;
							write_objects.push(ProtocolObject::from_id(image));
						}
						ImageData::Png(data) => {
							let nsdata: *const objc2_foundation::NSData = msg_send![class!(NSData), dataWithBytes:data.as_ptr() as *const c_void length:data.len() as u64];
							if nsdata.is_null() {
								return Err(Error::Unknown {
									description: "Failed to create NSData from bytes".into(),
								});
							}
							let item = objc2_app_kit::NSPasteboardItem::new();
							item.setData_forType(&*(nsdata as *const NSData), NSPasteboardTypePNG);
							write_objects.push(ProtocolObject::from_id(item));
						}
						ImageData::Svg(data) => {
							let item = objc2_app_kit::NSPasteboardItem::new();
							item.setString_forType(
								&NSString::from_str(&data),
								&NSString::from_str(NS_PASTEBOARD_TYPE_SVG),
							);
							write_objects.push(ProtocolObject::from_id(item));
						}
					},
					ClipboardData::FileUrl(urls) => {
						for url in urls.iter() {
							let url = url_encode::encode_path_to_uri(url);
							let item = objc2_app_kit::NSPasteboardItem::new();
							item.setString_forType(
								&NSString::from_str(&url),
								NSPasteboardTypeFileURL,
							);
							write_objects.push(ProtocolObject::from_id(item));
						}
					}
					ClipboardData::Special((format_name, data)) => {
						let nsdata: *const objc2_foundation::NSData = msg_send![class!(NSData), dataWithBytes:data.as_ptr() as *const c_void length:data.len() as u64];
						if nsdata.is_null() {
							return Err(Error::Unknown {
								description: "Failed to create NSData from bytes".into(),
							});
						}
						main_item.setData_forType(
							&*(nsdata as *const NSData),
							&NSString::from_str(format_name),
						);
						has_main_item_data = true;
					}
					_ => {}
				}
			}

			// Add the main item first if it has data
			if has_main_item_data {
				write_objects.insert(0, ProtocolObject::from_id(main_item));
			}

			if write_objects.is_empty() {
				return Ok(());
			}

			if !self.clipboard.pasteboard.writeObjects(&NSArray::from_vec(write_objects)) {
				return Err(Error::Unknown {
					description: "NSPasteboard#writeObjects: returned false".into(),
				});
			}
			Ok(())
		})?;

		Ok(())
	}
}

pub(crate) struct Clear<'clipboard> {
	clipboard: &'clipboard mut Clipboard,
}

impl<'clipboard> Clear<'clipboard> {
	pub(crate) fn new(clipboard: &'clipboard mut Clipboard) -> Self {
		Self { clipboard }
	}

	pub(crate) fn clear(self) -> Result<(), Error> {
		self.clipboard.clear();
		Ok(())
	}
}
