# training/
# Data flywheel: Claude acts as teacher, local models are students.
# When a local model fails or is uncertain, Claude labels the example.
# Those labels accumulate as training data for future local model retraining.
# This is knowledge distillation — the student gradually needs the teacher less.
