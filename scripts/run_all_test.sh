SEED_ARRAY=(13 43 83 181 271 347 433 659 727 859)

ITER=$1

for SEED in ${SEED_ARRAY[@]}
do
    bash $HOME/nvidia-bert/run_test.sh ${SEED}
done