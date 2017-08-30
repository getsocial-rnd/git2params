QUIET := true

.PHONY: package
package: clean
	@echo "=== Building new package in the Docker container"
ifeq ($(QUIET),true)
	@docker build -q -t pck -f Dockerfile.package . && docker run -it --rm  -v $(PWD)/package:/package pck /bin/bash -c "zip -q -r9 /package/app.zip ."
else
	@docker build -t pck -f Dockerfile.package . && docker run -it --rm  -v $(PWD)/package:/package pck /bin/bash -c "zip -r9 /package/app.zip ."
endif

clean:
	@echo "=== Cleaning old package"
	@rm -rf package

deploy: package
ifdef ENVIRONMENT
	@echo "=== Deploying lambda function"
	sls deploy -v -s ${ENVIRONMENT}
else
	@echo "\nERROR: please define ENVIRONMENT variable" 
endif

invoke: deploy
ifdef ENVIRONMENT
	@echo "=== Invoking lambda function"
	sls invoke -f main -l -s ${ENVIRONMENT}
else
	@echo "\nERROR: please define ENVIRONMENT variable" 
endif

local:
ifdef ENVIRONMENT
	@echo "=== Invoking lambda function locally"
	sls invoke local -f main --stage ${ENVIRONMENT}
else
	@echo "\nERROR: please define ENVIRONMENT variable" 
endif
