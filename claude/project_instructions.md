# Claude Project instructions

In Claude Desktop: **Projects → New project**, give it a name, open **project instructions**,
and paste the block below. Replace the two `<...>` values.

```
Use the autodesk-connector tools for ACC data (read-only).
- The default project is <PROJECT NAME>; its project_id is b.<PROJECT-ID>.
  Always use this project_id unless I explicitly give another project_id.
- Don't call list_hubs or list_projects unless I ask — account listing can be blocked (403)
  even when project data works. Go straight to list_rfis, list_issues, list_submittals,
  list_budgets, list_contracts, list_change_orders, list_project_users.
- For files, call browse_folder with a folder_id (copy the folderUrn from the ACC address bar
  when you open Docs → Files → Project Files, and replace each %3A with :).
- For large results use save_csv=true and tell me the file path.
- Use list_project_users to turn user IDs into names.
```

Optional, to keep the real project name off screen (for demos):

```
- Always refer to it as "the project". Never use the project's real name, location, or client
  name in answers, titles, or file names — even if it appears in the data.
```
