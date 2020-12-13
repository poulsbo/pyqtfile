"""
PyQTFile
========

A Python library for reading, modfying and writing QuickTime movies.

A QuickTime movie consists of a list of atoms. An atom can contain other
atoms, allowing for complex data structures.

All atoms have a type, and the structure of atom data is specific to each type.
By registering additional atom classes, this data can then be deserialized, modified 
and serialized as needed.

When reading an existing movie and no atom class is found for a type, the 
PassthroughAtom class is used. This lazily passes through the source data,
which allows manipulation of a movie with only partial understanding of the
atoms it contains.

Atoms provide list-like behaviour for their children, and dict-like behaviour
for fields.

Some atoms ("stco", etc) contain file offsets, so it may not be safe to remove
or add atoms unless these offsets are recalculated. When removing atoms, a simpler
route is use free().

Usage
-----

	>>> import qtfile
	>>> import qtatoms
	>>> for atom in qtfile.QuickTimeFile('mymovie.mov', atom_modules=[qtatoms]):
	>>>		print atom, atom.fields

"""

__author__ = "Niklas Aldergren <niklas@aldergren.com>"

import logging
import struct
import os
import string

LOG = logging.getLogger("qtfile")


class QuickTimeFile(list):
	"""A QuickTime movie."""

	def __init__(self, source=None, atom_classes=None, atom_modules=None):
		"""Initialize QuickTime movie. To directly read an existing movie, 
		the source parameter can be either a path or a file-like object.

		The atom_handlers parameter can be used to register additional
		type-specific classes. The atom_modules has the same purpose, but
		will find and register all appropriate classes in the given modules.
		"""
		if atom_classes:
			self.atom_classes = atom_classes
		else:
			self.atom_classes = []

		if atom_modules:
			for module in atom_modules:
				self.register_module(module)

		if source:
			if isinstance(source, str):
				self.read(open(source, 'rb'))
			else:
				self.read(source)

	def register_class(self, cls):
		"""Register an atom class."""
		self.atom_classes.append(cls)

	def register_module(self, module):
		"""Register all atom classes in a module."""
		import inspect

		def is_handler_class(cls):
			# We don't want to register the base Atom class as a handler.
			return (inspect.isclass(cls) and
				    issubclass(cls, Atom) and
				    cls is not Atom and
				    cls.explicit_registration == False)

		for _, cls in inspect.getmembers(module, is_handler_class):
			self.register_class(cls)


	def read(self, stream):
		"""Read QuickTime movie from stream. The stream argument can be
		any file-like object that implements read(), tell() and seek()."""
		for a in self:
			self.remove(a)
		for a in Atom.read(stream, stream.tell(), 0, self, self.atom_classes):
			self.append(a)

	def write(self, stream):
		"""Write QuickTime movie to stream."""
		[atom.write(stream) for atom in self]

	def find(self, types):
		"""Find atoms of specific types in movie."""
		matches = []
		for atom in self:
			if atom.kind in types:
				matches.append(atom)
			matches.extend(atom.find(types))
		return matches


class Atom(list):
	"""Basic unit of data in QuickTime movies."""

	# This uses the name "kind" in place of "type", to avoid shadowing the built-in type().

	header = ">L4s"
	header_extsize = ">Q"
	supported_types = []
	container = False
	field_defs = []

	# Set to True to allow a trailing null at the end of the atom (used by some containers).
	trailing_null = False

	# Force all immediate children of this atom to be handled by a specific class.
	force_child_class = None

	# Set to True to hide this class from register_module().
	explicit_registration = False

	def __init__(self, kind=""):
		"""Initialize Atom with a type."""
		super(Atom, self).__init__()
		self.kind = kind
		self.parent = None
		self.fields = {}
		self.extended_header = False

		# Indicates whether this atom should have a terminating null when serialized.
		self.terminating_null = False

	@property
	def safe_kind(self):
		"""Returns the atom type, in a safely printable format."""
		safe = ""
		for c in self.kind:
			if c in string.printable:
				safe += c
			else:
				safe += "?"
		return safe

	def __repr__(self):
		return "<%s %s>" % (self.__class__.__name__, self.safe_kind)

	@property
	def size(self):
		"""Calculate and return the size of this atom (including children)."""
		size = struct.calcsize(self.header)
		if self.extended_header:
			size += struct.calcsize(self.header_extsize)
		for _, format in self.field_defs:
			size += struct.calcsize(format)
		for child in self:
			size += child.size
		if self.terminating_null:
			size += 4
		return size

	@classmethod
	def supports_type(cls, kind):
		"""Returns True if this class can handle the given atom type."""
		return kind in cls.supported_types

	@classmethod
	def read(cls, stream, start=None, end=0, parent=None, atom_classes=None, force_class=None):
		"""Read atoms from stream.

		The start parameter indicates the offset at which to start reading. End
		indicates the offset at which to stop reading. This can also be set
		to 0 to continue until end-of-file, or -1 to stop after the first atom.
		"""
		atoms = []

		if not atom_classes:
			atom_classes = []

		if start != None and (stream.tell() != start):
			stream.seek(start)

		while end <= 0 or stream.tell() < end:

			atom = None
			handler = None
			offset = stream.tell()

			try:
				size, kind, extended = Atom.read_header(stream)

				debug("Found header %s (%s bytes)" % ([c for c in kind], size), ">", stream)


				if force_class:
					handler = force_class
				else:
					for c in atom_classes:
						if c.supports_type(kind):
							handler = c
							break

				if handler:
					atom = handler(kind)
					atom.read_data(stream, offset + size)

					if atom.container:

						for child in Atom.read(stream, stream.tell(), offset + size, atom, atom_classes, atom.force_child_class):
							atom.append(child)

					if size != atom.size:
						warning("Size mismatch [%s->%s], will not serialize correctly" % (size, atom.size), atom.safe_kind, stream)

				if atom == None:
					atom = PassthroughAtom(kind, stream, offset, size)

				atom.parent = parent
				atom.extended_header = extended

				debug("Instanced with %s" % atom.__class__.__name__, atom.safe_kind, stream)

			except QuickTimeEOF:
				debug("End of file, stopped reading", "?", stream)
				break

			except QuickTimeParseError as e:
				error(e.message, "?", stream)
				error("Parse error, stopped reading", "?", stream)
				break

			# This will let us continue reading the stream when encountering partially read atoms. 
			# This is to be expected with the passthrough atom as it's not actually reading anything.

			if stream.tell() != (offset + size):
				debug("Partial read, seeking ahead to %d" % (offset + size), atom.safe_kind, stream)
				stream.seek(offset + size)

			# If we're the last item in a container with a terminating null, consume it.
			# TODO: Document this properly. Why are we looking at the container?
			if parent != None and isinstance(parent, Atom) and parent.trailing_null and (end - stream.tell() == 4):
				debug("Terminating null found", atom.safe_kind, stream)
				parent.terminating_null = True
				stream.read(4)

			atoms.append(atom)

			if end == -1:
				break

		return atoms

	@classmethod
	def read_header(self, stream):
		"""Read atom header from stream. Returns (size, kind, extended).
		Extended will be True if the header uses the extended size field."""
		size, kind = read_struct(stream, self.header)
		extended_header = False

		if not kind:
			raise QuickTimeParseError("Atom with null type", stream.tell())

		if size == 1:
			debug("Reading extended size field", kind, stream)
			size = read_struct(stream, self.header_extsize)
			extended_header = True

		elif size == 0:
			# TODO: If the size field is set to 0, this is the last atom and extends until eof.
			raise QuickTimeParseError("Atoms of size 0 is unsupported", stream.tell())

		return size, kind, extended_header

	def read_data(self, stream, end):
		"""Read and parse atom data."""
		for key, format in self.field_defs:
			self.fields[key] = read_struct(stream, format)

	def write(self, stream, recursive=True):
		"""Write atom to stream. If recursive is set to False, child atoms
		will not be written. If this is used, write_end() must also be called
		as appropriate."""
		offset = stream.tell()
		self.write_header(stream)
		self.write_data(stream, recursive)

		if recursive:
			self.write_end(stream)

			# If we're not recursing, there's no point in checking the length of the write.
			if stream.tell() - offset != self.size:
				warning("Partial write [%d->%d], file will probably be corrupt" % (self.size, stream.tell() - offset), self.kind, stream)

	def write_header(self, stream):
		"""Write atom header to stream."""
		if self.extended_header or self.size > 2**32:
			debug("Writing extended size header", self.kind, stream)
			stream.write(struct.pack(self.header, 1, self.kind))
			stream.write(struct.pack(self.header_extsize, self.size))
		else:
			debug("Writing header", self.kind, stream)
			stream.write(struct.pack(self.header, self.size, self.kind))

	def write_data(self, stream, recursive=True):
		"""Write atom data to stream."""
		debug("Serializing data", self.kind, stream)
		for key, format in self.field_defs:
			stream.write(struct.pack(format, self.fields[key]))

	def write_end(self, stream):
		"""Write terminating null to stream, if needed."""
		if self.terminating_null:
			debug("Writing terminating null", self.kind, stream)
			stream.write("\x00"*4)

	def find(self, types, recursive=True):
		"""Find atoms of specific types in atom."""
		matches = []
		for child in self:
			if child.kind in types:
				matches.append(child)
			if recursive:
				matches.extend(child.find(types, recursive=True))
		return matches

	def free(self):
		"""Convert Atom to free."""
		# FIXME: This should also zero all the fields.
		self.kind = "free"

	# Implements some dict-like behaviour for atom fields.

	def __getitem__(self, key):
		if isinstance(key, int):
			return super(Atom, self).__getitem__(key)
		else:
			return self.fields[key]

	def __setitem__(self, key, value):
		if isinstance(key, int):
			return super(Atom, self).__setitem__(key, value)
		else:
			self.fields[key] = value

	def keys(self):
		return list(self.fields.keys())

	def values(self):
		return list(self.fields.values())

	def items(self):
		return list(self.fields.items())

	def has_key(self, key):
		return key in self.fields


class PassthroughAtom(Atom):
	"""A placeholder atom without knowledge of the actual data structure,
	instead lazily passes through source data without parsing."""

	def __init__(self, kind, source, offset, size):
		"""Initialize a passthrough atom."""
		super(PassthroughAtom, self).__init__(kind)
		self._source = source
		self._offset = offset
		self._size = size

	def write(self, stream, recursive=True):
		"""Write atom data to stream. As this just passes through the
		source data, the recursive parameter has no meaning here."""
		debug("Passing through data", self.kind, stream)
		self._source.seek(self._offset)
		stream.write(self._source.read(self._size))

	def __repr__(self):
		return "<%s %s %sb>" % (self.__class__.__name__, self.kind, self.size)

	@property
	def size(self):
		# Unlike other atoms, we always return a fixed size here.
		return self._size


def debug(message, scope, stream):
	if stream:
		position = stream.tell()
	else:
		position = 0
	LOG.debug("@%-10d | [%-4s] %s" % (position, scope, message))


def error(message, scope, stream):
	if stream:
		position = stream.tell()
	else:
		position = 0
	LOG.error("@%-10d | [%-4s] %s" % (position, scope, message))


def warning(message, scope, stream):
	if stream:
		position = stream.tell()
	else:
		position = 0
	LOG.warning("@%-10d | [%-4s] %s" % (position, scope, message))


def read_struct(stream, format, unwrap=True):
	"""Read and unpack structured data from a stream. Raises QuickTimeParseError
	if there's not enough data or it has an incorrect format. If unwrap is set,
	tuples with a single value will be unwrapped before returning."""
	need_bytes = struct.calcsize(format)
	buf = stream.read(need_bytes)
	if len(buf) == 0:
		raise QuickTimeEOF()
	elif len(buf) != need_bytes:
		raise QuickTimeParseError("Expected %d bytes, got %d" % (need_bytes, len(buf)), stream.tell())
	try:
		result = struct.unpack(format, buf)
		if unwrap and len(result) == 1:
			return result[0]
		return result
	except struct.error:
		raise QuickTimeParseError("Could not unpack data", stream.tell())


class QuickTimeParseError(Exception):
	"""Raised if an error is encountered during parsing of a QuickTime movie."""
	def __init__(self, message, offset = 0):
		Exception.__init__(self, message)
		self.message = message
		self.offset = offset

	def __str__(self):
		return "@%d: %s" % (self.offset, self.message)


class QuickTimeEOF(Exception):
	"""Raised if EOF is encountered during parsing of a QuickTime movie."""
	pass

