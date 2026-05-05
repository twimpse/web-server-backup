# web-server-backup
A website backup script that supports wordpress and static sites. 

The script scans for apache websites, then checks if those websites are wordpress or not. Then it creates a backup file for each website. 
It can then copy the files to a remote backup location.

It is also possible to change settings for each website, but that requires some setup and adding the configuration options to the /etc/web-server-backup.yaml file. 
