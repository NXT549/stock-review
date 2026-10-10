-- Stock Analyzer database schema (SQLite dialect; generated from
-- src/stock_analyzer/storage/models.py via SQLAlchemy).
-- PostgreSQL is also supported (config storage.db_url); types map cleanly.
-- NOTE: SQLite only honors the ON DELETE CASCADEs when the connection sets
-- PRAGMA foreign_keys=ON (Storage does this via an engine connect event).

CREATE TABLE runs (
	id INTEGER NOT NULL, 
	run_date DATE NOT NULL, 
	universe_name VARCHAR(64) NOT NULL, 
	config_hash VARCHAR(64) NOT NULL, 
	n_tickers INTEGER NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_run_date_universe UNIQUE (run_date, universe_name)
);
CREATE INDEX ix_runs_config_hash ON runs (config_hash);
CREATE INDEX ix_runs_run_date ON runs (run_date);

CREATE TABLE securities (
	ticker VARCHAR(24) NOT NULL, 
	name VARCHAR(256), 
	sector VARCHAR(128), 
	industry VARCHAR(128), 
	exchange VARCHAR(64), 
	currency VARCHAR(16), 
	first_seen DATE NOT NULL, 
	last_seen DATE NOT NULL, 
	PRIMARY KEY (ticker)
);

CREATE TABLE raw_snapshots (
	id INTEGER NOT NULL, 
	run_id INTEGER NOT NULL, 
	ticker VARCHAR(24) NOT NULL, 
	payload JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE CASCADE
);
CREATE INDEX ix_raw_snapshots_ticker ON raw_snapshots (ticker);
CREATE INDEX ix_raw_snapshots_run_id ON raw_snapshots (run_id);

CREATE TABLE metric_values (
	id INTEGER NOT NULL, 
	run_id INTEGER NOT NULL, 
	ticker VARCHAR(24) NOT NULL, 
	metric_key VARCHAR(48) NOT NULL, 
	raw_value FLOAT, 
	is_available BOOLEAN NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE CASCADE
);
CREATE INDEX ix_metric_values_run_id ON metric_values (run_id);
CREATE INDEX ix_metric_values_ticker ON metric_values (ticker);
CREATE INDEX ix_metricvalue_run_ticker ON metric_values (run_id, ticker);

CREATE TABLE metric_scores (
	id INTEGER NOT NULL, 
	run_id INTEGER NOT NULL, 
	ticker VARCHAR(24) NOT NULL, 
	metric_key VARCHAR(48) NOT NULL, 
	percentile FLOAT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE CASCADE
);
CREATE INDEX ix_metric_scores_run_id ON metric_scores (run_id);
CREATE INDEX ix_metric_scores_ticker ON metric_scores (ticker);
CREATE INDEX ix_metricscore_run_ticker ON metric_scores (run_id, ticker);

CREATE TABLE category_scores (
	id INTEGER NOT NULL, 
	run_id INTEGER NOT NULL, 
	ticker VARCHAR(24) NOT NULL, 
	category VARCHAR(32) NOT NULL, 
	score FLOAT, 
	coverage INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE CASCADE
);
CREATE INDEX ix_category_scores_run_id ON category_scores (run_id);
CREATE INDEX ix_categoryscore_run_ticker ON category_scores (run_id, ticker);
CREATE INDEX ix_category_scores_ticker ON category_scores (ticker);

CREATE TABLE scores (
	id INTEGER NOT NULL, 
	run_id INTEGER NOT NULL, 
	run_date DATE NOT NULL, 
	ticker VARCHAR(24) NOT NULL, 
	final_score FLOAT, 
	rank INTEGER, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_score_run_ticker UNIQUE (run_id, ticker), 
	FOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE CASCADE
);
CREATE INDEX ix_scores_ticker ON scores (ticker);
CREATE INDEX ix_score_date_ticker ON scores (run_date, ticker);
CREATE INDEX ix_scores_run_date ON scores (run_date);
CREATE INDEX ix_scores_run_id ON scores (run_id);

CREATE TABLE reports (
	id INTEGER NOT NULL, 
	run_id INTEGER NOT NULL, 
	fmt VARCHAR(16) NOT NULL, 
	path VARCHAR(512) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE CASCADE
);
CREATE INDEX ix_reports_run_id ON reports (run_id);
