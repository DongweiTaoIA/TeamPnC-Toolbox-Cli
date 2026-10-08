I have a ssrs project for reports

C:\repos\tfs\DP_Unifi_Resources\

it has multiple eviroment 

FNCT
for example reports is in 
C:\repos\tfs\DP_Unifi_Resources\Dev\_FNCT\SSRS\DPReports\Src

ACCP
PROD

if I change a report I need deploy the report to server
for example:
DPR928E.rdl

in FNCT and ACCP enviroment, I am able to deploy reports with current window login

but for PROD, I need a specail user:
iap.iafg.net/lidot
other user second part is different
password is temporary for one time use only

I want to create a tool to deploy the reports for me, something like:

sssr-deploy -FNCT -r: DPR928E, DPR928F (dpr928e, no case sensitive)

if it is PROD

sssr-deploy -PROD -u:lidot(or iap.iafg.net/lidot) -p:abcd123 -r:DPR928E, DPR928F

to deploy
1. pull new changes
2. build the solution depends on enviroment
3. find deploy ssrs server in the solution based on the enviroment

I also want to disply the process and log
