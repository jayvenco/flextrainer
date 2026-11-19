# Changelog

All notable changes to FlexTrainer. Format: [Keep a Changelog](https://keepachangelog.com), versions follow SemVer.

## [0.1.0] - 2026-11-19
### Added
- Exams overview as the main menu, with progress, accuracy, review and "resume" per exam.
- Import questions from PDF, Word (.docx), Markdown and text files, and scrape web pages.
- Heuristic question parser (lettered options, answer lines, answer keys, checkboxes) plus optional ChatGPT-assisted import.
- Practice sessions with instant correct/wrong feedback; sessions are saved and can be resumed.
- ChatGPT check of every question: answer comparison, short explanation and a suggested reference link.
- Login (default `admin`/`admin` with forced change), 5 themes, 5 bundled fonts, PWA, keyboard shortcuts.
- Weekly backups (SQLite online backup), export, import/restore with safety copy.
- Health/readiness endpoints, structured logs with a log viewer, About page with update notice.
- Unraid template, install script, GHCR publishing workflow.
