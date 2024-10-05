F_VALUES=(25000)
LEARNING_RATE=0.01
NUM_CLIENTS=2

python fast_main3.py --dataset=cifar10 --num-clients=$NUM_CLIENTS --f=$F --lr=$LEARNING_RATE --log-to-tensorboard=cifar_CNN > "output/\$OUTPUT_LOG" 2> "error/\$ERROR_LOG"
