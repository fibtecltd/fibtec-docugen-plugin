resource "aws_sqs_queue" "jobs" {
  name       = "sessions-jobs.fifo"
  fifo_queue = true
}

resource "aws_ecs_task_definition" "box" {
  family                   = "box"
  requires_compatibilities = ["FARGATE"]
  cpu                      = 2048
  memory                   = 8192
}
