# Auto-deploy to EC2 via SSM — one-time setup

This workflow (`deploy.yml`) pushes new commits to the production EC2 box
(`15.206.56.206`, `ap-south-1`) on every push to `main`. It uses AWS SSM
Send-Command, so **no SSH port needs to be opened and no key needs to be
shared.** GitHub Actions only talks to AWS APIs.

You do this setup once. After that, every backend / frontend change deploys
automatically when a PR merges to `main`. A manual trigger
(`workflow_dispatch`) is also wired up for re-deploys.

---

## 1. Create the deploy IAM user

Open IAM in the AWS Console (Mumbai region is fine; IAM is global):

1. **Users → Create user**
   - Name: `github-actions-deploy`
   - Access type: **Programmatic access only** (no console login)
2. **Attach policy → Create policy → JSON tab**, paste this and replace
   `<ACCOUNT_ID>` and `<INSTANCE_ID>` with your real values
   (instance ID looks like `i-0abc1234…`):

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Sid": "AllowSendDeployCommand",
         "Effect": "Allow",
         "Action": ["ssm:SendCommand"],
         "Resource": [
           "arn:aws:ec2:ap-south-1:<ACCOUNT_ID>:instance/<INSTANCE_ID>",
           "arn:aws:ssm:ap-south-1::document/AWS-RunShellScript"
         ]
       },
       {
         "Sid": "AllowReadCommandResults",
         "Effect": "Allow",
         "Action": [
           "ssm:GetCommandInvocation",
           "ssm:ListCommandInvocations",
           "ssm:DescribeInstanceInformation"
         ],
         "Resource": "*"
       }
     ]
   }
   ```

   Name the policy `GitHubActionsDeployPolicy` and attach it to the
   `github-actions-deploy` user.

3. Open the user → **Security credentials → Create access key**.
   - Pick "Application running outside AWS" → Next → Create.
   - Save the **Access key ID** and **Secret access key** — you only see
     the secret once.

---

## 2. Confirm the EC2 instance has SSM agent + role

The instance needs:

- **SSM agent installed.** Ubuntu 18.04+ has it preinstalled. Verify on
  the box: `systemctl status amazon-ssm-agent`.
- **Instance profile with `AmazonSSMManagedInstanceCore`.** In EC2
  Console → select the instance → Security → IAM Role. If it's missing
  the SSM core policy, attach it (Modify IAM role).

Quick sanity check from CloudShell (with region set to ap-south-1):

```bash
aws ssm describe-instance-information \
  --filters "Key=InstanceIds,Values=<INSTANCE_ID>" \
  --query "InstanceInformationList[0].PingStatus" --output text
```

Should print `Online`. If it prints `ConnectionLost` or returns empty,
the agent is offline or the IAM role is missing — fix that first; the
workflow's "Verify instance reachability" step would catch this anyway.

---

## 3. Find the instance ID

If you don't already have it noted:

```bash
aws ec2 describe-instances \
  --region ap-south-1 \
  --filters "Name=ip-address,Values=15.206.56.206" \
  --query "Reservations[].Instances[].InstanceId" \
  --output text
```

Result looks like `i-0abc1234567890def`.

---

## 4. Add secrets to the GitHub repo

GitHub → repo → **Settings → Secrets and variables → Actions → New repository secret**.

Add three secrets:

| Name                    | Value                                       |
| ----------------------- | ------------------------------------------- |
| `AWS_ACCESS_KEY_ID`     | from step 1                                 |
| `AWS_SECRET_ACCESS_KEY` | from step 1                                 |
| `EC2_INSTANCE_ID`       | from step 3 (e.g. `i-0abc1234567890def`)    |

Done.

---

## 5. Verify the wiring

Trigger a manual run to confirm before the next push:

1. GitHub repo → **Actions** tab → "Deploy to EC2 via SSM" workflow.
2. **Run workflow** → branch `main` → leave `force_recreate` as `true` → Run.
3. Watch the job. Expected timeline:
   - `Verify instance reachability` — ~5s
   - `Send deploy command via SSM` — ~5s
   - `Wait for deploy to finish` — 2-4 min (Docker rebuild)
   - `Report deploy output` — prints the EC2 box's stdout (git log, container status, backend health probe)
   - `Verify production endpoints` — calls `/api/commodity/overview` from GitHub's runner; prints lane titles + symbol list.

The last step is the "yes it deployed correctly" signal. Lane title
should read `MP+OF Futures` (single element). Symbols list should be the
8-instrument universe once you've also seeded that via
`PUT /api/commodity/strategy-agent/config`.

---

## What gets deployed

The workflow's deploy step runs (on the EC2 box, as user `ubuntu`):

```bash
cd /home/ubuntu/nomad-curie     # falls back to /opt/TradeBot if missing
git fetch origin
git log HEAD..origin/main --oneline
git pull origin main --ff-only
docker compose up -d --build --force-recreate backend frontend
sleep 30
docker compose ps
curl http://localhost:8000/health
curl http://localhost:8000/api/commodity/overview | summarise
```

`--ff-only` means it'll refuse to merge if there's any divergent local
history. If a previous SSM-tarball deploy left local commits on the box
that aren't on `origin/main`, you'll see the workflow fail with "not
possible to fast-forward, aborting" — that's the safety net. To fix:
SSH/SSM in once and `git reset --hard origin/main`, then re-trigger.

---

## Path filter

The workflow ignores doc-only changes and other sibling projects. It
re-runs only when one of these paths changes in a push:

```
backend/**
frontend/**
docker-compose.yml
.github/workflows/deploy.yml
```

Add more paths if other directories also need to land on the box.

---

## Rolling back

If a deploy ships a regression: revert the bad commit and push to main.
The same workflow runs on the revert commit and brings prod back to a
good state. No manual SSH needed.

For really-bad-emergency rollback, GitHub → Actions → previous good run
→ **Re-run all jobs**. That re-deploys the older commit SHA (because the
workflow checks out the ref the run was triggered on).
