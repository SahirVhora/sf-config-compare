# Security

SF Config Compare is designed for local or access-controlled use with your own
SAP SuccessFactors tenants.

Do not publish real tenant URLs, company IDs, usernames, passwords, OAuth client
secrets, generated reports, SQLite databases, logs, or exported configuration
data. The `.gitignore` excludes the local runtime files used by the app.

If you deploy the Flask app to a public host, put it behind your own
authentication and network controls before adding tenant credentials or pulling
configuration data.
