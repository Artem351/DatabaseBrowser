"""SQL Editor tab: execute, load more rows, count, commit, and roll back"""

from dataclasses import replace
from threading import Event

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QSpinBox, QSplitter, QTableView, QVBoxLayout, QWidget,
)

from .grid import QueryTableModel
from .models import QueryResult, TransactionResult
from .services import DEFAULT_CHUNK_SIZE, QueryManager


class SqlEditorTab(QWidget):
    schema_changed = pyqtSignal()

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.result = None
        self._connection_key = None
        self._pending_operation = None
        self._cancellation = None
        self._pending_count = False
        self._stop_requested = False
        self._transaction_unknown = False
        self._in_transaction = False

        self.active_label = QLabel('Connect a database to execute SQL')
        self.active_label.setWordWrap(True)
        self.editor = QPlainTextEdit()
        self.editor.setObjectName('sql_editor')
        self.editor.setPlaceholderText('Enter one SQL statement, then press Ctrl+Enter')
        self.editor.textChanged.connect(self._update_buttons)
        self.btn_run = QPushButton('Run SQL')
        self.btn_run.clicked.connect(self.run_query)
        self.btn_next = QPushButton('Load next batch')
        self.btn_next.clicked.connect(self.load_next_chunk)
        self.btn_stop = QPushButton('Stop loading')
        self.btn_stop.clicked.connect(self.stop_viewing)
        self.btn_commit = QPushButton('Commit')
        self.btn_commit.clicked.connect(lambda: self.finish_transaction(True))
        self.btn_rollback = QPushButton('Roll back')
        self.btn_rollback.clicked.connect(lambda: self.finish_transaction(False))
        self.batch_size = QSpinBox()
        self.batch_size.setRange(1, 5000)
        self.batch_size.setValue(DEFAULT_CHUNK_SIZE)
        self.batch_size.setToolTip('Batch size applies to the next SQL statement')
        buttons = QHBoxLayout()
        for button in (self.btn_run, self.btn_next, self.btn_stop):
            buttons.addWidget(button)
        buttons.addWidget(QLabel('Rows per batch:'))
        buttons.addWidget(self.batch_size)
        buttons.addStretch()
        buttons.addWidget(self.btn_commit)
        buttons.addWidget(self.btn_rollback)

        self.model = QueryTableModel(self)
        self.grid = QTableView()
        self.grid.setObjectName('query_results')
        self.grid.setModel(self.model)
        self.grid.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.grid.setAlternatingRowColors(True)
        self.grid.horizontalHeader().setDefaultSectionSize(160)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.editor)
        split.addWidget(self.grid)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        self.statistics_label = QLabel('No query has been executed')
        self.statistics_label.setWordWrap(True)
        self.count_label = QLabel()
        self.count_label.setWordWrap(True)
        self.transaction_label = QLabel('Use Commit or Roll back to finish a pending transaction')
        self.transaction_label.setWordWrap(True)
        self.message_label = QLabel()
        self.message_label.setObjectName('query_message')
        self.message_label.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(self.active_label)
        layout.addLayout(buttons)
        layout.addWidget(split, 1)
        layout.addWidget(self.statistics_label)
        layout.addWidget(self.count_label)
        layout.addWidget(self.transaction_label)
        layout.addWidget(self.message_label)
        shortcut = QShortcut(QKeySequence('Ctrl+Return'), self)
        shortcut.activated.connect(self.run_query)

        controller.connection_changed.connect(self._connection_changed)
        controller.busy_changed.connect(self._update_buttons)
        controller.operation_result.connect(self._operation_result)
        controller.operation_failed.connect(self._operation_failed)
        controller.operation_finished.connect(self._operation_finished)
        self._connection_changed(controller.active)
        self._update_buttons()

    def _clear_result(self):
        self.result = None
        self._pending_count = self._stop_requested = False
        self.model.reset_result()
        self.statistics_label.setText('No query has been executed')
        self.count_label.clear()
        self.message_label.clear()

    def _connection_changed(self, active):
        key = (active.profile.id, active.file_path, active.read_only) if active else None
        if key != self._connection_key:
            self._connection_key = key
            self._clear_result()
            self._in_transaction = self._transaction_unknown = False
        self.active_label.setText(
            f'SQL in {active.file_path}' + (' (read-only)' if active.read_only else '')
            if active else 'Connect a database to execute SQL'
        )
        self._update_buttons()

    def _update_buttons(self, *_):
        idle = not self.controller.busy and not self.controller._closing
        connected = self.controller.active is not None
        self.btn_run.setEnabled(connected and idle and bool(self.editor.toPlainText().strip()))
        self.btn_next.setEnabled(connected and idle and self.result is not None and self.result.has_more)
        loading = self._pending_operation in {'query.execute', 'query.fetch', 'query.count'}
        more = self.result is not None and self.result.has_more
        self.btn_stop.setEnabled(connected and not self.controller._closing and (loading or (idle and more)))
        transaction = self._in_transaction or self._transaction_unknown
        self.btn_commit.setEnabled(connected and idle and transaction)
        self.btn_rollback.setEnabled(connected and idle and transaction)
        self.batch_size.setEnabled(idle)

    def _submit(self, name, operation):
        event = Event()
        self._pending_operation = name
        self._cancellation = event
        accepted = self.controller.submit_database_operation(
            name, lambda manager: operation(QueryManager(manager.executor), event)
        )
        if not accepted:
            self._pending_operation = None
            self._cancellation = None
        self._update_buttons()
        return accepted

    def run_query(self):
        if not self.btn_run.isEnabled():
            return
        sql = self.editor.toPlainText()
        chunk_size = self.batch_size.value()
        if self._submit('query.execute', lambda manager, event: manager.execute_query(
                sql, chunk_size=chunk_size, cancellation=event)):
            self._clear_result()
            self.message_label.setText('Executing SQL…')

    def load_next_chunk(self):
        if not self.btn_next.isEnabled():
            return
        result_id = self.result.result_id
        self._submit('query.fetch', lambda manager, event: manager.fetch_next_chunk(result_id, event))

    def stop_viewing(self):
        self._pending_count = False
        if self._pending_operation is not None:
            self._stop_requested = True
            self.cancel_pending()
            self.message_label.setText('Stopping…')
        elif self.result is not None and self.result.has_more:
            result_id = self.result.result_id
            self._submit('query.stop', lambda manager, event: manager.stop_viewing(result_id))

    def cancel_pending(self):
        if self._cancellation is not None:
            self._cancellation.set()

    def finish_transaction(self, commit):
        button = self.btn_commit if commit else self.btn_rollback
        if not button.isEnabled():
            return
        self._pending_count = False
        self._submit('query.commit' if commit else 'query.rollback',
                     lambda manager, event: manager.finish_transaction(commit=commit))

    def _show_statistics(self):
        if self.result is None:
            return
        stats = self.result.statistics
        affected = 'n/a' if stats.affected_rows is None else str(stats.affected_rows)
        self.statistics_label.setText(
            f'Execute: {stats.execution_seconds:.4f} s | Fetch: {stats.fetch_seconds:.4f} s | '
            f'Loaded: {self.model.rowCount()} | Affected: {affected}'
        )
        if stats.is_count_calculated:
            self.count_label.setText(f'Total rows: {stats.total_count} | Count: {stats.count_seconds:.4f} s')
        elif stats.count_error:
            self.count_label.setText(f'Total rows unavailable: {stats.count_error}')
        elif not self.result.columns:
            self.count_label.setText('This statement does not return rows')
        elif self._pending_operation == 'query.count' or self._pending_count:
            self.count_label.setText('Calculating total rows…')
        elif not self.result.can_count and self.result.has_more:
            self.count_label.setText('Total rows will be available after all returned rows are loaded')
        else:
            self.count_label.setText('Total rows: not calculated')
        self.transaction_label.setText(
            'Uncommitted transaction — use Commit or Roll back'
            if self._in_transaction else 'No pending transaction'
        )

    def _operation_result(self, name, result):
        if not name.startswith('query.'):
            return
        if isinstance(result, TransactionResult):
            self._in_transaction = result.in_transaction
            self._transaction_unknown = False
            if self.result is not None:
                self.result = replace(self.result, has_more=False, incomplete=self.result.has_more)
            self.message_label.setText('Changes committed' if name == 'query.commit' else 'Changes rolled back')
            self.transaction_label.setText('No pending transaction')
            if result.schema_changed:
                self.schema_changed.emit()
            return
        if not isinstance(result, QueryResult):
            return
        if name != 'query.execute' and (self.result is None or result.result_id != self.result.result_id):
            return
        if name == 'query.execute':
            self.model.reset_result(result.columns)
        try:
            self.model.append_rows(result.rows)
        except (OSError, MemoryError) as error:
            self.result = result
            self._stop_requested = True
            self.message_label.setText(f'Cannot cache this batch: {error}; already loaded rows are kept')
            return
        self.result = result
        self._in_transaction = result.statistics.in_transaction
        self._transaction_unknown = False
        if name == 'query.execute':
            self._pending_count = result.has_more and result.can_count
            if result.statistics.schema_changed:
                self.schema_changed.emit()
        if name == 'query.stop':
            self.message_label.setText('Loading stopped; already loaded rows are still available')
        elif name != 'query.count':
            self.message_label.setText(
                'Batch loaded — request more rows when needed' if result.has_more
                else 'Statement completed' if not result.columns
                else 'Empty result' if not self.model.rowCount() else 'All rows loaded'
            )
        self._show_statistics()
        self._update_buttons()

    def _operation_failed(self, name, message):
        if not name.startswith('query.'):
            return
        self.message_label.setText(message)
        self._pending_count = False
        self._transaction_unknown = True
        if self.result is not None and name in {'query.fetch', 'query.stop', 'query.commit', 'query.rollback'}:
            self.result = replace(self.result, has_more=False, incomplete=True)
            self.message_label.setText(message + '\nResult incomplete; already loaded rows are kept')
        self.transaction_label.setText('Check the transaction; use Commit or Roll back if needed')
        self._update_buttons()

    def _operation_finished(self, name, success):
        if name == self._pending_operation:
            self._pending_operation = self._cancellation = None
        if name == 'connect' and success:
            # Reconnecting to the same file also replaces its connection and cursor
            self._clear_result()
            self._in_transaction = self._transaction_unknown = False
        if name.startswith('schema.') and not name.endswith('refresh') and self.result is not None:
            self._pending_count = False
            self.result = replace(self.result, has_more=False, incomplete=self.result.has_more)
            self.message_label.setText('Result closed for a schema change; loaded rows are still available')
        if self.controller.busy or self.controller._closing:
            self._update_buttons()
            return
        if self._stop_requested:
            self._stop_requested = False
            if self.result is not None and self.result.has_more:
                self.stop_viewing()
        elif self._pending_count and self.result is not None:
            self._pending_count = False
            result_id = self.result.result_id
            self._submit('query.count', lambda manager, event: manager.get_total_count(result_id, event))
        self._update_buttons()

    def close_cache(self):
        self.model.close_cache()
