# ─────────────────────────────────────────────────────────────────────
#  Network
#
#  One public subnet for the legacy SQL Server host, two private subnets
#  for RDS and DMS.
#
#  Two private subnets because RDS requires a subnet group spanning at
#  least two availability zones — even for a single-AZ instance. AWS wants
#  somewhere to fail over to if you ever enable Multi-AZ.
#
#  No NAT gateway. Nothing in the private subnets needs to reach the
#  internet, and a NAT gateway costs ~$1/day for the privilege of doing
#  nothing here.
# ─────────────────────────────────────────────────────────────────────

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block = var.vpc_cidr

  # Both required for RDS to get a resolvable private DNS name.
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = var.project
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${var.project}-igw"
  }
}

# ─── Public subnet — the legacy SQL Server host ──────────────────────

resource "aws_subnet" "public" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 1) # 10.20.1.0/24
  availability_zone = data.aws_availability_zones.available.names[0]

  # A public IP so the SSM agent can phone home. This is what lets us
  # get a shell without opening port 22 to the world.
  map_public_ip_on_launch = true

  tags = {
    Name = "${var.project}-public"
    tier = "public"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name = "${var.project}-public-rt"
  }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# ─── Private subnets — RDS and DMS ───────────────────────────────────

resource "aws_subnet" "private" {
  count = 2

  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 11 + count.index) # 10.20.11.0/24, 10.20.12.0/24
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = {
    Name = "${var.project}-private-${count.index + 1}"
    tier = "private"
  }
}

# No routes to the internet. Local VPC traffic only — which is the
# entire point of calling it private.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${var.project}-private-rt"
  }
}

resource "aws_route_table_association" "private" {
  count = 2

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

resource "aws_db_subnet_group" "main" {
  name       = "${var.project}-subnet-group"
  subnet_ids = aws_subnet.private[*].id

  tags = {
    Name = "${var.project}-subnet-group"
  }
}
