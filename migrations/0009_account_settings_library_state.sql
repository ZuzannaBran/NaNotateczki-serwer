-- The application exposes libraryState in account settings. The original
-- account_settings CHECK predates this category; never edit 0007 in place.
ALTER TABLE account_settings
    DROP CONSTRAINT account_settings_category_check;

ALTER TABLE account_settings
    ADD CONSTRAINT account_settings_category_check
    CHECK (category IN (
        'appPreferences',
        'editorPreferences',
        'recentColors',
        'backupPreferences',
        'libraryState'
    ));
