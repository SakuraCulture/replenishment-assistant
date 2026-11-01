"""Compact shared amount strip for the store overview and store review page."""
from PySide6.QtWidgets import QWidget, QFrame, QHBoxLayout, QVBoxLayout
from ui_controls import label
import review_amounts as money


class ReviewAmounts(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(3)
        row = QHBoxLayout();self.values = {};self.details = {}
        for key,title,help_text in [('manual','人工改量商品最终金额（元）',money.MANUAL_HELP),('new','新品商品最终金额（元）',money.NEW_HELP)]:
            card = QFrame();card.setObjectName('card');card.setToolTip(help_text)
            body = QVBoxLayout(card);body.setContentsMargins(12,6,12,6);body.setSpacing(2)
            line = QHBoxLayout();line.addWidget(label(title,'muted'));line.addStretch()
            value = label('—');value.setStyleSheet('font-size:22px;font-weight:600;color:#176C63;');line.addWidget(value)
            body.addLayout(line);detail = label('尚无有效结果','muted');body.addWidget(detail)
            self.values[key] = value;self.details[key] = detail;row.addWidget(card,1)
        layout.addLayout(row);layout.addWidget(label(money.NOTE,'muted'))

    def set_summary(self, value=None):
        for key in self.values:
            self.values[key].setText(money.amount_text(value,key) if value is not None else '—')
            self.details[key].setText((money.count_text(value,key)+(' · '+money.delta_text(value) if key=='manual' else ' · 包含未安排的0数量候选')) if value is not None else '尚无有效结果')
