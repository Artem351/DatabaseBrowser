"""A result grid with a temporary disk cache and a small cache in memory"""

import pickle
from bisect import bisect_right
from collections import OrderedDict
from tempfile import TemporaryFile

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt


class QueryTableModel(QAbstractTableModel):
    """Keep previously loaded rows available without keeping them all in RAM"""

    def __init__(self, parent=None, *, cache_batches: int = 4):
        super().__init__(parent)
        self.columns = ()
        self._file = TemporaryFile(mode='w+b')
        self._batches = []
        self._starts = []
        self._rows = 0
        self._cache = OrderedDict()
        self._cache_batches = cache_batches
        self.destroyed.connect(lambda: self._file.close())

    def reset_result(self, columns=()):
        self.beginResetModel()
        self._file.seek(0)
        self._file.truncate()
        self.columns = tuple(columns)
        self._batches.clear()
        self._starts.clear()
        self._rows = 0
        self._cache.clear()
        self.endResetModel()

    def append_rows(self, rows):
        if not rows:
            return
        rows = tuple(rows)
        # Write before announcing new rows so a full disk cannot leave a half-added batch
        self._file.seek(0, 2)
        offset = self._file.tell()
        pickle.dump(rows, self._file, protocol=pickle.HIGHEST_PROTOCOL)
        self._file.flush()
        self.beginInsertRows(QModelIndex(), self._rows, self._rows + len(rows) - 1)
        batch = len(self._batches)
        self._starts.append(self._rows)
        self._batches.append(offset)
        self._rows += len(rows)
        self._remember(batch, rows)
        self.endInsertRows()

    def _remember(self, batch, rows):
        self._cache[batch] = rows
        self._cache.move_to_end(batch)
        while len(self._cache) > self._cache_batches:
            self._cache.popitem(last=False)

    def row_values(self, row: int):
        if not 0 <= row < self._rows:
            raise IndexError(row)
        batch = bisect_right(self._starts, row) - 1
        if batch not in self._cache:
            self._file.seek(self._batches[batch])
            # Only data written by this model is loaded from its private temporary file
            self._remember(batch, pickle.load(self._file))
        else:
            self._cache.move_to_end(batch)
        return self._cache[batch][row - self._starts[batch]]

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else self._rows

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role not in {Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole}:
            return None
        value = self.row_values(index.row())[index.column()]
        if value is None:
            return 'NULL'
        if isinstance(value, bytes):
            return f'BLOB ({len(value)} bytes): {value[:32].hex()}'
        text = str(value)
        limit = 1024 if role == Qt.ItemDataRole.ToolTipRole else 256
        return text if len(text) <= limit else text[:limit] + '…'

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.columns[section] if 0 <= section < len(self.columns) else None
        return section + 1

    def close_cache(self):
        self._cache.clear()
        self._file.close()
